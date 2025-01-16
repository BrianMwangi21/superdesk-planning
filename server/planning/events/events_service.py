import pytz
import itertools

from copy import deepcopy
from bson import ObjectId
from typing import Any, AsyncGenerator, cast
from datetime import datetime, timedelta
from apps.archive.common import get_auth
from apps.auth import get_user, get_user_id

from superdesk.utc import utcnow
from superdesk.core import get_app_config
from superdesk import get_resource_service
from superdesk.resource_fields import ID_FIELD
from superdesk.errors import SuperdeskApiError
from superdesk.metadata.item import GUID_NEWSML
from superdesk.notification import push_notification
from superdesk.core.utils import date_to_str, generate_guid


from planning import signals
from planning.types import (
    PLANNING_RELATED_EVENT_LINK_TYPE,
    EventResourceModel,
    PlanningRelatedEventLink,
    PlanningSchedule,
    PostStates,
    UpdateMethods,
    WorkflowState,
)
from planning.types.event import EmbeddedPlanning
from planning.common import (
    WorkflowStates,
    format_address,
    get_event_max_multi_day_duration,
    get_max_recurrent_events,
    remove_lock_information,
    set_ingested_event_state,
    post_required,
    update_post_item,
)
from planning.events.events_history_async_service import EventsHistoryAsyncService
from planning.planning import PlanningAsyncService
from planning.core.service import BasePlanningAsyncService
from planning.utils import (
    get_planning_event_link_method,
    get_related_event_ids_for_planning,
    get_related_planning_for_events,
)

from .events_sync import sync_event_metadata_with_planning_items
from .events_utils import (
    generate_recurring_dates,
    get_events_embedded_planning,
    get_recurring_timeline,
)


class EventsAsyncService(BasePlanningAsyncService[EventResourceModel]):
    resource_name = "events"

    async def get_expired_items(
        self, expiry_datetime: datetime, spiked_events_only: bool = False
    ) -> AsyncGenerator[list[dict[str, Any]], None]:
        """
        Retrieve "expired" events which are those whose end date is on or before `expiry_datetime` and
        are not already marked as expired.

        By default, items returned are:
        - Not expired.
        - Have an end date `<= expiry_datetime`.

        If `spiked_events_only` is True, only spiked events are returned, still filtered by
        end date `<= expiry_datetime`.

        Results are sorted by start date and fetched in batches.
        """
        query: dict[str, Any] = {
            "query": {
                "bool": {
                    "must_not": [{"term": {"expired": True}}],
                    "filter": {"range": {"dates.end": {"lte": date_to_str(expiry_datetime)}}},
                },
            },
            "sort": [{"dates.start": "asc"}],
            "size": get_max_recurrent_events(),
        }

        if spiked_events_only:
            del query["query"]["bool"]["must_not"]
            query["query"]["bool"]["must"] = [{"term": {"state": WorkflowState.SPIKED}}]

        total_received = 0
        total_events = -1

        while True:
            query["from"] = total_received

            results = await self.search(query)
            items = await results.to_list_raw()
            results_count = len(items)

            # If the total_events has not been set, then this is the first query
            # In which case we need to store the total hits from the search
            if total_events < 0:
                total_events = results_count

                # If the search doesn't contain any results, return here
                if total_events < 1:
                    break

            # If the last query doesn't contain any results, return here
            if results_count == 0:
                break

            total_received += results_count

            # Yield the results for iteration by the callee
            yield items

    def _extract_embedded_planning(
        self, docs: list[EventResourceModel]
    ) -> list[tuple[EventResourceModel, list[EmbeddedPlanning]]]:
        """
        Extracts out the ``embedded_planning`` for a list of given events
        """

        embedded_planning_lists: list[tuple[EventResourceModel, list[EmbeddedPlanning]]] = []
        for event in docs:
            embedded_planning = get_events_embedded_planning(event)
            if len(embedded_planning):
                embedded_planning_lists.append((event, embedded_planning))

        return embedded_planning_lists

    def _synchronise_associated_plannings(
        self, embedded_planning_list: list[tuple[EventResourceModel, list[EmbeddedPlanning]]]
    ):
        """
        Synchronise/process the given associated Planning item(s)
        """
        for event, embedded_planning in embedded_planning_list:
            sync_event_metadata_with_planning_items(None, event.to_dict(), embedded_planning)

    async def create(self, docs: list[EventResourceModel]):
        """
        Extracts out the ``embedded_planning`` before saving the Event(s)
        And then uses them to synchronise/process the associated Planning item(s)
        """

        docs = await self._convert_dicts_to_model(docs)

        embedded_planning_list = self._extract_embedded_planning(docs)
        await self.prepare_events_data(docs)
        ids = await super().create(docs)

        self._synchronise_associated_plannings(embedded_planning_list)

        return ids

    async def prepare_events_data(self, docs: list[EventResourceModel]) -> None:
        """
        Prepares basic attributes of events before creation. This method generates recurring events
        if applicable, sets up planning schedules, and links events to planning items.

        Args:
            events (list[EventResourceModel]): A list of event models to prepare.

        Returns:
            None: Modifies the input list in-place.
        """
        generated_events = []
        for event in docs:
            if not event.guid:
                event.guid = generate_guid(type=GUID_NEWSML)
            event.id = event.guid

            if not event.language:
                event.language = event.languages[0] if len(event.languages) > 0 else get_app_config("DEFAULT_LANGUAGE")

            # TODO-ASYNC: consider moving this into base service later
            event.original_creator = ObjectId(get_user_id()) or None

            # overwrite expiry date if needed
            self._overwrite_event_expiry_date(event)

            # we ignore the 'update_method' on create
            if event.update_method:
                event.update_method = None

            # remove the 'expired' flag if it is set, as no new Event can be created as expired
            if event.expired:
                event.expired = False

            event.planning_schedule = self._create_planning_schedule(event)
            original_planning_item = event.planning_item

            self.validate_event(event)

            # If _created_externally is true, generate_recurring_events is restricted.
            if event.dates and event.dates.recurring_rule and not event.dates.recurring_rule._created_externally:
                recurring_events = self._generate_recurring_events(event)
                generated_events.extend(recurring_events)

                # remove the event that contains the recurring rule. We don't need it anymore
                docs.remove(event)

                # Set the current Event to the first Event in the new series
                # This will make sure the ID of the Event can be used when
                # using 'event' from here on, such as when linking to a Planning item
                event = recurring_events[0]

                # And set the Planning Item from the original
                # (generate_recurring_events removes this field)
                event.planning_item = original_planning_item

            if event.state == WorkflowStates.INGESTED:
                events_history = EventsHistoryAsyncService()
                await events_history.on_item_created([event.to_dict()])

            if original_planning_item:
                await self._link_to_planning(event)
                event.planning_item = None

        if generated_events:
            docs.extend(generated_events)

    async def on_created(self, docs: list[EventResourceModel]):
        """Send WebSocket Notifications for created Events

        Generate the list of IDs for recurring and non-recurring events,
        then send this list off to the clients so they can fetch these events
        """
        notifications_sent = []
        history_service = EventsHistoryAsyncService()

        for doc in docs:
            event_id = doc.id

            # If we duplicated this event, update the history
            if doc.duplicate_from:
                parent_id = doc.duplicate_from
                parent_event = await self.find_by_id(parent_id)
                if not parent_event:
                    raise SuperdeskApiError.badRequestError("Parent event not found")

                await history_service.on_item_updated({"duplicate_id": event_id}, parent_event.to_dict(), "duplicate")
                await history_service.on_item_updated({"duplicate_id": parent_id}, doc.to_dict(), "duplicate_from")

                duplicate_ids = parent_event.duplicate_to or []
                duplicate_ids.append(event_id)

                await super().update(parent_id, {"duplicate_to": duplicate_ids})

            event_type = "events:created"
            user_id = doc.original_creator or ""

            if doc.recurrence_id:
                event_type = "events:created:recurring"
                event_id = str(doc.recurrence_id)

            # Don't send notification if one has already been sent
            # This is to ensure recurring events don't send multiple notifications
            if event_id in notifications_sent or doc.previous_recurrence_id:
                continue

            notifications_sent.append(event_id)
            push_notification(event_type, item=event_id, user=user_id)

    async def on_update(self, updates: dict[str, Any], original: EventResourceModel):
        """Update single or series of recurring events.

        Determine if the supplied event is a single event or a
        series of recurring events, and call the appropriate method
        for the event type.
        """
        if "skip_on_update" in updates:
            # this is a recursive update (see below)
            del updates["skip_on_update"]
            return

        update_method = updates.pop("update_method", UpdateMethods.SINGLE)
        user = get_user()
        user_id = user.get(ID_FIELD) if user else None

        if user_id:
            updates["version_creator"] = user_id
            set_ingested_event_state(updates, original.to_dict())

        lock_user = original.lock_user or None
        str_user_id = str(user.get(ID_FIELD)) if user_id else None

        if lock_user and str(lock_user) != str_user_id:
            raise SuperdeskApiError.forbiddenError("The item was locked by another user")

        # If only the `recurring_rule` was provided, then fill in the rest from the original
        # this can happen, for example, when converting a single Event to a series of Recurring Events
        if list(updates.get("dates") or {}) == ["recurring_rule"]:
            new_dates = original.to_dict()["dates"]
            new_dates.update(updates["dates"])
            updates["dates"] = new_dates

        # validate event
        self.validate_event(updates, original)

        # Run the specific methods based on if the original is a single or a series of recurring events
        if not getattr((original.dates or {}), "recurring_rule") or update_method == UpdateMethods.SINGLE:
            await self._update_single_event(updates, original)
        else:
            await self._update_recurring_events(updates, original, update_method)

        return await super().on_update(updates, original)

    async def update(self, event_id: str | ObjectId, updates: dict[str, Any], etag: str | None = None):
        """Updates the event and also extracts out the ``embedded_planning`` before saving the Event
        And then uses them to synchronise/process the associated Planning item(s)
        """

        updates.setdefault("versioncreated", utcnow())
        original_event = await self.find_by_id(event_id)

        if original_event is None:
            raise SuperdeskApiError.badRequestError("Event not found")

        # Extract the ``embedded_planning`` from the updates
        embedded_planning = get_events_embedded_planning(updates)

        await super().update(event_id, updates, etag)

        # Process ``embedded_planning`` field, and sync Event metadata with associated Planning/Coverages
        sync_event_metadata_with_planning_items(original_event.to_dict(), updates, embedded_planning)

    async def on_updated(self, updates: dict[str, Any], original: EventResourceModel, from_ingest: bool = False):
        # if this Event was converted to a recurring series
        # then update all associated Planning items with the recurrence_id
        if updates.get("recurrence_id") and not original.recurrence_id:
            await PlanningAsyncService().on_event_converted_to_recurring(updates, original)

        if not updates.get("duplicate_to"):
            posted = update_post_item(updates, original.to_dict())
            if posted:
                new_event = await self.find_by_id(original.id)
                assert new_event is not None
                updates["_etag"] = new_event.etag
                updates["state_reason"] = new_event.state_reason

        if original.lock_user and "lock_user" in updates and updates.get("lock_user") is None:
            # when the event is unlocked by the patch.
            push_notification(
                "events:unlock",
                item=str(original.id),
                user=str(get_user_id()),
                lock_session=str(get_auth().get("_id")),
                etag=updates["_etag"],
                recurrence_id=original.recurrence_id or None,
                from_ingest=from_ingest,
            )

        await self.delete_event_files(updates, original.files)

        if "location" not in updates and original.location:
            updates["location"] = original.location

        updates[ID_FIELD] = original.id
        self._enhance_event_item(updates)

    async def delete_event_files(self, updates: dict[str, Any], event_files: list[ObjectId]):
        files = [f for f in event_files if f not in (updates or {}).get("files", [])]
        files_service = get_resource_service("events_files")

        for file in files:
            events_using_file = await self.find({"files": file})
            if (await events_using_file.count()) == 0:
                files_service.delete_action(lookup={"_id": file})

    async def on_deleted(self, doc: EventResourceModel):
        push_notification(
            "events:delete",
            item=str(doc.id),
            user=str(get_user_id()),
            lock_session=str(get_auth().get("_id")),
        )

    def validate_event(
        self, updated_event: dict[str, Any] | EventResourceModel, original_event: EventResourceModel | None = None
    ):
        """Validate the event"""

        if isinstance(updated_event, dict):
            updated_event = EventResourceModel.from_dict(updated_event)
            # mypy complains even when `from_dict` returns a model instance
            updated_event = cast(EventResourceModel, updated_event)

        self._validate_multiday_event_duration(updated_event)
        self._validate_dates(updated_event, original_event)
        self._validate_convert_to_recurring(updated_event, original_event)
        self._validate_template(updated_event, original_event)

        # TODO-ASYNC: migrate `sanitize_input_data` to support new models based on pydantic
        # this function below allows both Event and Planning items
        # sanitize_input_data(updates)

    def _validate_multiday_event_duration(self, event: EventResourceModel):
        """Validate that the multiday event duration is not greater than PLANNING_MAX_MULTI_DAY_DURATION

        @:param dict event: event created or updated
        """
        max_duration = get_event_max_multi_day_duration()
        if not max_duration > 0:
            return

        if not event.dates:
            return

        assert event.dates.start is not None
        assert event.dates.end is not None

        event_duration = event.dates.end - event.dates.start
        if event_duration.days > max_duration:
            raise SuperdeskApiError(message="Event duration is greater than {} days.".format(max_duration))

    def _validate_dates(self, updated_event: EventResourceModel, original_event: EventResourceModel | None = None):
        """Validate the dates

        @:param dict event:
        """
        # TODO-ASYNC: consider/check if these validations should be in the pydantic model
        event = updated_event if updated_event.dates or not original_event else original_event

        assert event.dates is not None

        start_date = event.dates.start
        end_date = event.dates.end

        if not start_date or not end_date:
            raise SuperdeskApiError(message="Event START DATE and END DATE are mandatory.")

        if end_date < start_date:
            raise SuperdeskApiError(message="END TIME should be after START TIME")

        if event.dates.recurring_rule and not event.dates.recurring_rule.until and not event.dates.recurring_rule.count:
            raise SuperdeskApiError(message="Recurring event should have an end (until or count)")

    def _validate_convert_to_recurring(
        self, updated_event: dict[str, Any] | EventResourceModel, original: EventResourceModel | None = None
    ):
        """Validates if the convert to recurring action is valid.

        :param updates:
        :param original:
        :return:
        """
        if original is None:
            return

        if isinstance(updated_event, dict):
            updated_event = EventResourceModel.from_dict(updated_event)
            updated_event = cast(EventResourceModel, updated_event)

        if (
            original.lock_action == "convert_recurring"
            and updated_event.dates
            and updated_event.dates.recurring_rule is None
        ):
            raise SuperdeskApiError(message="Event recurring rules are mandatory for convert to recurring action.")

        if original.lock_action == "convert_recurring" and original.recurrence_id:
            raise SuperdeskApiError(message="Event is already converted to recurring event.")

    @staticmethod
    def _validate_template(updated_event: EventResourceModel, original_event: EventResourceModel | None = None):
        """Ensures that event template can't be changed

        :param updates: updates to event that should be saved
        :type updates: dict
        :param original: original event before update
        :type original: dict
        :return:
        """
        if original_event is None:
            return

        # we can't change `template` id
        if updated_event.template and updated_event.template != original_event.template:
            raise SuperdeskApiError.badRequestError(
                message="Request is not valid",
                payload={"template": "This value can't be changed."},
            )

    async def _update_single_event(self, updates: dict[str, Any], original: EventResourceModel):
        """Updates the metadata of a single event.

        If recurring_rule is provided, we convert this single event into
        a series of recurring events, otherwise we simply update this event.
        """

        if post_required(updates, original.to_dict()):
            merged: EventResourceModel = original.clone_with(updates)

            # TODO-ASYNC: replace when `event_post` is async and validate_item is available for use
            # get_resource_service("events_post").validate_item(merged.to_dict())

        # Determine if we're to convert this single event to a recurring of events
        if original.lock_action == "convert_recurring" and updates.get("dates", {}).get("recurring_rule") is not None:
            generated_events = await self._convert_to_recurring_events(updates, original)

            # if the original event was "posted" then post all the generated events
            if original.pubstatus in [PostStates.CANCELLED, PostStates.USABLE]:
                post = {
                    "event": generated_events[0].id,
                    "etag": generated_events[0].etag,
                    "update_method": "all",
                    "pubstatus": original.pubstatus,
                }

                # TODO-ASYNC: replace when `event_post` is async
                get_resource_service("events_post").post([post])

            push_notification(
                "events:updated:recurring",
                item=str(original.id),
                user=str(updates.get("version_creator", "")),
                recurrence_id=str(generated_events[0].recurrence_id),
            )
        else:
            if original.lock_action == "mark_completed" and updates.get("actioned_date"):
                self.mark_event_complete(updates, original, False)

            # This updates Event metadata only
            push_notification(
                "events:updated",
                item=str(original.id),
                user=str(updates.get("version_creator", "")),
            )

    async def _update_recurring_events(
        self, updates: dict[str, Any], original: EventResourceModel, update_method: UpdateMethods
    ):
        """Method to update recurring events.

        If the recurring_rule has been removed for this event, process
        it separately, otherwise update the event and/or its recurring rules
        """
        # This method now only handles updating of Event metadata
        # So make sure to remove any date information that might be in
        # the updates
        updates.pop("dates", None)
        original_as_dict = original.to_dict()

        if update_method == UpdateMethods.FUTURE:
            historic, past, future = await get_recurring_timeline(original_as_dict)
            events = future
        else:
            historic, past, future = await get_recurring_timeline(original_as_dict)
            events = historic + past + future

        events_post_service = get_resource_service("events_post")

        # First we want to validate that all events can be posted
        for e in events:
            if post_required(updates, e):
                merged = deepcopy(e)
                merged.update(updates)
                events_post_service.validate_item(merged)

        # If this update is from assignToCalendar action
        # Then we only want to update the calendars of each Event
        only_calendars = original.lock_action == "assign_calendar"
        original_calendar_qcodes = [calendar.qcode for calendar in original.calendars]

        # Get the list of calendars added
        updated_calendars = [
            calendar for calendar in updates.get("calendars") or [] if calendar["qcode"] not in original_calendar_qcodes
        ]

        mark_completed = original.lock_action == "mark_completed" and updates.get("actioned_date")
        mark_complete_validated = False
        for e in events:
            event_id = e[ID_FIELD]

            new_updates = deepcopy(updates)
            new_updates["skip_on_update"] = True
            new_updates[ID_FIELD] = event_id

            if only_calendars:
                # Get the original for this item, and add new calendars to it
                # Skipping calendars already assigned to this item
                original_event: EventResourceModel = await self.find_by_id(event_id)
                assert original_event is not None
                original_qcodes = [calendar.qcode for calendar in original_event.calendars]

                new_updates["calendars"] = deepcopy(original_event.calendars)
                new_updates["calendars"].extend(
                    [calendar for calendar in updated_calendars if calendar["qcode"] not in original_qcodes]
                )
            elif mark_completed:
                ev = EventResourceModel.from_dict(e)
                self.mark_event_complete(updates, ev, mark_complete_validated)
                # It is validated if the previous funciton did not raise an error
                mark_complete_validated = True

            # Remove ``embedded_planning`` before updating this event, as this should only be handled
            # by the event provided to this update request
            new_updates.pop("embedded_planning", None)

            await signals.events_update.send(new_updates, original)

        # And finally push a notification to connected clients
        push_notification(
            "events:updated:recurring",
            item=str(original[ID_FIELD]),
            recurrence_id=str(original.recurrence_id),
            user=str(updates.get("version_creator", "")),
        )

    def mark_event_complete(self, updates: dict[str, Any], event: EventResourceModel, mark_complete_validated: bool):
        assert event.dates is not None
        assert event.dates.start is not None

        # If the entire series is in future, raise an error
        if event.recurrence_id:
            if not mark_complete_validated:
                if event.dates.start.date() > updates["actioned_date"].date():
                    raise SuperdeskApiError.badRequestError("Recurring series has not started.")

            # If we are marking an event as completed
            # Update only those which are behind the 'actioned_date'
            if event.dates.start < updates["actioned_date"]:
                return

        for plan in get_related_planning_for_events([event.id], "primary"):
            if plan.get("state") != WorkflowState.CANCELLED and len(plan.get("coverages", [])) > 0:
                # TODO-ASYNC: replace when `planning_cancel` is async
                get_resource_service("planning_cancel").patch(
                    plan[ID_FIELD],
                    {
                        "reason": "Event Completed",
                        "cancel_all_coverage": True,
                    },
                )

    async def _convert_to_recurring_events(self, updates: dict[str, Any], original: EventResourceModel):
        """
        Convert a single event to a series of recurring events and stores them into database.
        This also triggers the `signals.events_created` signal.
        """

        self._validate_convert_to_recurring(updates, original)
        updates["recurrence_id"] = original.id

        merged = original.clone_with(updates)

        # Generated new events will be "draft"
        merged.state = WorkflowState.DRAFT
        generated_events = self._generate_recurring_events(merged, updates["recurrence_id"])
        updated_event = generated_events.pop(0)

        assert updated_event.dates is not None
        assert updated_event.dates.start is not None
        assert original.dates is not None
        assert original.dates.start is not None

        # Check to see if the first generated event is different from original
        # If yes, mark original as rescheduled with generated recurrence_id
        if updated_event.dates.start.date() != original.dates.start.date():
            # Reschedule original event
            updates["update_method"] = UpdateMethods.SINGLE
            updates["dates"] = updated_event.dates
            updates["_planning_schedule"] = [x.to_dict() for x in self._create_planning_schedule(updated_event)]

            event_reschedule_service = get_resource_service("events_reschedule")
            event_reschedule_service.update_single_event(updates, original)

            if updates.get("state") == WorkflowState.RESCHEDULED:
                history_service = EventsHistoryAsyncService()
                await history_service.on_reschedule(updates, original.to_dict())
        else:
            # Original event falls as a part of the series
            # Remove the first element in the list (the current event being updated)
            # And update the start/end dates to be in line with the new recurring rules
            updates["dates"]["start"] = updated_event.dates.start
            updates["dates"]["end"] = updated_event.dates.end
            updates["_planning_schedule"] = [x.to_dict() for x in self._create_planning_schedule(updated_event)]
            remove_lock_information(item=updates)

        # create the new events
        embedded_planning_list = self._extract_embedded_planning(generated_events)
        await super().create(generated_events)
        self._synchronise_associated_plannings(embedded_planning_list)

        # signal's listener will generate these events' history
        await signals.events_created.send(generated_events)

        return generated_events

    def _set_planning_schedule(self, event: EventResourceModel):
        if event.dates and event.dates.start:
            event.planning_schedule = [PlanningSchedule(scheduled=event.dates.start)]

    def _create_planning_schedule(self, event: EventResourceModel) -> list[PlanningSchedule]:
        if event.dates and event.dates.start:
            return [PlanningSchedule(scheduled=event.dates.start)]
        return []

    def _overwrite_event_expiry_date(self, event: EventResourceModel):
        if event.expiry:
            assert event.dates is not None
            assert event.dates.end is not None

            expiry_minutes = get_app_config("PLANNING_EXPIRY_MINUTES", None)
            event.expiry = event.dates.end + timedelta(minutes=expiry_minutes or 0)

    def set_recurring_mode(self, event: EventResourceModel):
        assert event.dates is not None
        assert event.dates.recurring_rule is not None

        end_repeat_mode = event.dates.recurring_rule.end_repeat_mode

        if end_repeat_mode == "count":
            event.dates.recurring_rule.until = None
        elif end_repeat_mode == "until":
            event.dates.recurring_rule.count = None

    def _reset_recurring_event_fields(self, event: EventResourceModel):
        """
        Reset fields that are not required by the new (recurring) events
        """
        fields_to_reset = ["lock_user", "lock_time", "lock_session", "lock_action"]

        for field in fields_to_reset:
            setattr(event, field, None)

    def _generate_recurring_events(
        self, event: EventResourceModel, recurrence_id: str | None = None
    ) -> list[EventResourceModel]:
        """
        Generate recurring events based on the recurrence rules of the given event.

        Args:
            event (EventResourceModel): The original event used as a template for recurrence.
            recurrence_id (str, optional): The ID of the recurrence group. Defaults to None.

        Returns:
            list[EventResourceModel]: A list of newly generated recurring events.
        """
        assert event.dates is not None

        self.set_recurring_mode(event)
        generated_events = []

        assert event.dates.start is not None
        assert event.dates.end is not None
        assert event.dates.recurring_rule is not None

        # compute the difference between start and end in the original event
        time_delta = event.dates.end - event.dates.start

        max_recurring_events = get_max_recurrent_events()
        recurring_dates = generate_recurring_dates(
            start=event.dates.start,
            tz=pytz.timezone(event.dates.tz or ""),
            **event.dates.recurring_rule.to_dict(),
        )

        # for all the dates based on the recurring rules
        # set a limit to prevent too many events to be created
        recurring_dates_iter = itertools.islice(recurring_dates, 0, max_recurring_events)
        for i, date in enumerate(recurring_dates_iter):
            # prepare data for new recurring event
            new_id = generate_guid(type=GUID_NEWSML)
            recurring_event_updates = {"dates": dict(start=date, end=(date + time_delta)), "guid": new_id, "id": new_id}

            if not recurrence_id:
                recurring_event_updates["recurrence_id"] = new_id

            # reset fields not required by new events
            fields_to_reset = [
                "lock_user",
                "lock_time",
                "lock_session",
                "lock_action",
                "planning_schedule",
                "reschedule_from_schedule",
                "planning_item",
                "pubstatus",
                "reschedule_from",
            ]
            for field in fields_to_reset:
                recurring_event_updates[field] = None

            # let's finally clone the original event & update it with recurring event data
            new_event = event.clone_with(recurring_event_updates)

            # reset embedded_planning to all Events but the first one, as this auto-generates
            # associated Planning item with Coverages to the event
            if i > 0:
                new_event.embedded_planning = []

            # set expiry date
            self._overwrite_event_expiry_date(new_event)
            self._set_planning_schedule(new_event)

            generated_events.append(new_event)

        return generated_events

    @staticmethod
    async def _link_to_planning(event: EventResourceModel):
        """
        Links an Event to an existing Planning Item

        The Planning item remains locked, it is up to the client to release this lock
        after this operation is complete
        """
        planning_service = PlanningAsyncService()
        planning_item = await planning_service.find_by_id(event.planning_item)

        if not planning_item:
            raise SuperdeskApiError.badRequestError("Planning item not found")

        updates = {"related_events": planning_item.get("related_events") or []}
        event_link_method = get_planning_event_link_method()
        link_type: PLANNING_RELATED_EVENT_LINK_TYPE = (
            "primary"
            if not len(get_related_event_ids_for_planning(planning_item, "primary"))
            and event_link_method in ("one_primary", "one_primary_many_secondary")
            else "secondary"
        )
        related_planning = PlanningRelatedEventLink(_id=event.id, link_type=link_type)
        updates["related_events"].append(related_planning)

        # Add ``recurrence_id`` if the supplied Event is part of a series
        if event.recurrence_id:
            related_planning["recurrence_id"] = event.recurrence_id
            if not planning_item.get("recurrence_id") and link_type == "primary":
                updates["recurrence_id"] = event.recurrence_id

        # TODO-ASYNC: migrate `validate_on_update` method to async
        # planning_service.validate_on_update(updates, planning_item, get_user())
        await planning_service.system_update(event.planning_item, updates)

        await signals.planning_update.send(updates, planning_item)

    def _enhance_event_item(self, doc: dict[str, Any]):
        plannings = get_related_planning_for_events([doc[ID_FIELD]])

        if len(plannings):
            doc["planning_ids"] = [planning.get("_id") for planning in plannings]

        for location in doc.get("location") or []:
            format_address(location)

        # this is to fix the existing events have original creator as empty string
        if not doc.get("original_creator"):
            doc.pop("original_creator", None)
