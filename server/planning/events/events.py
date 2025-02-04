# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2013, 2014 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Superdesk Events"""

from typing import Dict, Any, Optional, List, Tuple
import superdesk
import logging
import itertools
import copy
import pytz
import re
from datetime import timedelta
from eve.methods.common import resolve_document_etag
from eve.utils import config, date_to_str
from flask import current_app as app
from copy import deepcopy
from dateutil.rrule import (
    rrule,
    YEARLY,
    MONTHLY,
    WEEKLY,
    DAILY,
    MO,
    TU,
    WE,
    TH,
    FR,
    SA,
    SU,
)

from superdesk import get_resource_service
from superdesk.errors import SuperdeskApiError
from superdesk.metadata.utils import generate_guid
from superdesk.metadata.item import GUID_NEWSML
from superdesk.notification import push_notification
from superdesk.utc import get_date, utcnow
from apps.auth import get_user, get_user_id
from apps.archive.common import get_auth, update_dates_for
from superdesk.users.services import current_user_has_privilege

from planning.types import Event, EmbeddedPlanning, EmbeddedCoverageItem
from planning.common import (
    UPDATE_SINGLE,
    UPDATE_FUTURE,
    get_max_recurrent_events,
    WORKFLOW_STATE,
    ITEM_STATE,
    prepare_ingested_item_for_storage,
    remove_lock_information,
    format_address,
    update_post_item,
    post_required,
    POST_STATE,
    get_event_max_multi_day_duration,
    set_original_creator,
    set_ingested_event_state,
    LOCK_ACTION,
    sanitize_input_data,
    set_ingest_version_datetime,
    is_new_version,
    update_ingest_on_patch,
    TEMP_ID_PREFIX,
)
from .events_base_service import EventsBaseService
from .events_schema import events_schema
from .events_sync import sync_event_metadata_with_planning_items

logger = logging.getLogger(__name__)

FREQUENCIES = {"DAILY": DAILY, "WEEKLY": WEEKLY, "MONTHLY": MONTHLY, "YEARLY": YEARLY}
DAYS = {"MO": MO, "TU": TU, "WE": WE, "TH": TH, "FR": FR, "SA": SA, "SU": SU}

organizer_roles = {
    "eorol:artAgent": "Artistic agent",
    "eorol:general": "General organiser",
    "eorol:tech": "Technical organiser",
    "eorol:travAgent": "Travel agent",
    "eorol:venue": "Venue organiser",
}

# based on onclusive provided content fields for now
CONTENT_FIELDS = {
    "name",
    "definition_short",
    "definition_long",
    "links",
    "ednote",
    "subject",
    "anpa_category",
    "location",
    "event_contact_info",
}


def get_events_embedded_planning(event: Event) -> List[EmbeddedPlanning]:
    def get_coverage_id(coverage: EmbeddedCoverageItem) -> str:
        if not coverage.get("coverage_id"):
            coverage["coverage_id"] = TEMP_ID_PREFIX + "-" + generate_guid(type=GUID_NEWSML)
        return coverage["coverage_id"]

    return [
        EmbeddedPlanning(
            planning_id=planning.get("planning_id"),
            update_method=planning.get("update_method") or "single",
            coverages={get_coverage_id(coverage): coverage for coverage in planning.get("coverages") or []},
        )
        for planning in event.pop("embedded_planning", [])
    ]


def get_subject_str(subject: Dict[str, str]) -> str:
    return ":".join(
        [
            subject.get("name", ""),
            subject.get("qcode", ""),
            subject.get("scheme", ""),
            str(subject.get("translations", "")),
        ]
    )


def is_event_updated(new_item: Event, old_item: Event) -> bool:
    if new_item.get("name") != old_item.get("name"):
        return True
    new_subject = set([get_subject_str(subject) for subject in new_item.get("subject", [])])
    old_subject = set([get_subject_str(subject) for subject in old_item.get("subject", [])])
    if new_subject != old_subject:
        return True
    old_location = old_item.get("location", [])
    new_location = new_item.get("location", [])
    if new_location != old_location:
        return True
    return False


def get_user_updated_keys(event_id: str) -> set[str]:
    history_service = get_resource_service("events_history")
    updates = history_service.get_by_id(event_id)
    updated_keys = set()
    for update in updates:
        if not update.get("user_id"):
            continue
        if update.get("update"):
            updated_keys.update(update["update"].keys())
    return updated_keys


class EventsService(superdesk.Service):
    """Service class for the events model."""

    def post_in_mongo(self, docs, **kwargs):
        """Post an ingested item(s)"""

        for doc in docs:
            prepare_ingested_item_for_storage(doc)
            self._resolve_defaults(doc)
            set_ingest_version_datetime(doc)

        self.on_create(docs)
        resolve_document_etag(docs, self.datasource)
        ids = self.backend.create_in_mongo(self.datasource, docs, **kwargs)
        self.on_created(docs)
        return ids

    def patch_in_mongo(self, _id: str, document, original) -> Optional[Dict[str, Any]]:
        """Patch an ingested item onto an existing item locally"""
        prepare_ingested_item_for_storage(document)
        events_history = get_resource_service("events_history")
        events_history.on_item_updated(document, original, "ingested")

        content_fields = app.config.get("EVENT_INGEST_CONTENT_FIELDS", CONTENT_FIELDS)
        updated_keys = get_user_updated_keys(_id)
        for key in updated_keys:
            if key in document and key in content_fields and original.get(key):
                document[key] = original[key]

        set_planning_schedule(document)
        update_ingest_on_patch(document, original)
        response = self.backend.update_in_mongo(self.datasource, _id, document, original)
        self.on_updated(document, original, from_ingest=True)
        return response

    def is_new_version(self, new_item, old_item):
        return is_new_version(new_item, old_item) or is_event_updated(new_item, old_item)

    def ingest_cancel(self, item, feeding_service):
        """Ignore cancelling on ingest, this will happen in ``update_post_item``"""

        pass

    def on_fetched(self, docs):
        for doc in docs["_items"]:
            self._enhance_event_item(doc)

    def on_fetched_item(self, doc):
        self._enhance_event_item(doc)

    @staticmethod
    def get_plannings_for_event(event):
        return get_resource_service("planning").find(where={"event_item": event.get(config.ID_FIELD)})

    def _enhance_event_item(self, doc):
        plannings = self.get_plannings_for_event(doc)

        if plannings.count() > 0:
            doc["planning_ids"] = [planning.get("_id") for planning in plannings]

        for location in doc.get("location") or []:
            format_address(location)

        # this is to fix the existing events have original creator as empty string
        if not doc.get("original_creator"):
            doc.pop("original_creator", None)

    @staticmethod
    def has_planning_items(doc):
        return EventsService.get_plannings_for_event(doc).count() > 0

    def get_all_items_in_relationship(self, item):
        # Get recurring items
        if item.get("recurrence_id"):
            all_items = self.find(where={"recurrence_id": item.get("recurrence_id")})
            # Now, get associated planning items with the same recurrence
            return itertools.chain(
                all_items,
                get_resource_service("planning").find(where={"recurrence_id": item.get("recurrence_id")}),
            )
        else:
            # Get associated planning items
            return self.get_plannings_for_event(item)

    def on_locked_event(self, doc, user_id):
        self._enhance_event_item(doc)

    @staticmethod
    def set_ingest_provider_sequence(item, provider):
        """Sets the value of ingest_provider_sequence in item.

        :param item: object to which ingest_provider_sequence to be set
        :param provider: ingest_provider object, used to build the key name of sequence
        """
        sequence_number = get_resource_service("sequences").get_next_sequence_number(
            key_name="ingest_providers_{_id}".format(_id=provider[config.ID_FIELD]),
            max_seq_number=app.config["MAX_VALUE_OF_INGEST_SEQUENCE"],
        )
        item["ingest_provider_sequence"] = str(sequence_number)

    def on_create(self, docs):
        # events generated by recurring rules
        generated_events = []
        for event in docs:
            # generates an unique id
            if "guid" not in event:
                event["guid"] = generate_guid(type=GUID_NEWSML)
            event[config.ID_FIELD] = event["guid"]

            # SDCP-638
            if not event.get("language"):
                try:
                    event["language"] = event["languages"][0]
                except (KeyError, IndexError):
                    event["language"] = app.config["DEFAULT_LANGUAGE"]

            # family_id get on ingest we don't need it planning
            event.pop("family_id", None)

            # set the author
            set_original_creator(event)

            # set timestamps
            update_dates_for(event)

            # overwrite expiry date
            overwrite_event_expiry_date(event)

            # We ignore the 'update_method' on create
            if "update_method" in event:
                del event["update_method"]

            # Remove the 'expired' flag if it is set, as no new Event can be created
            # as expired
            if "expired" in event:
                del event["expired"]

            set_planning_schedule(event)
            planning_item = event.get("_planning_item")

            # validate event
            self.validate_event(event)

            # If _created_externally is true, generate_recurring_events is restricted.
            if event["dates"].get("recurring_rule", None) and not event["dates"]["recurring_rule"].get(
                "_created_externally", False
            ):
                event["dates"]["start"] = get_date(event["dates"]["start"])
                event["dates"]["end"] = get_date(event["dates"]["end"])
                recurring_events = generate_recurring_events(event)
                generated_events.extend(recurring_events)
                # remove the event that contains the recurring rule. We don't need it anymore
                docs.remove(event)  # todo: why we remove that event and not update it?

                # Set the current Event to the first Event in the new series
                # This will make sure the ID of the Event can be used when
                # using 'event' from here on, such as when linking to a Planning item
                event = recurring_events[0]
                # And set the Planning Item from the original
                # (generate_recurring_events removes this field)
                event["_planning_item"] = planning_item

            if event["state"] == "ingested":
                events_history = get_resource_service("events_history")
                events_history.on_item_created([event])

            if planning_item:
                self._link_to_planning(event)
                del event["_planning_item"]

        if generated_events:
            docs.extend(generated_events)

    def create(self, docs: List[Event], **kwargs):
        """Saves the list of Events to Mongo & Elastic

        Also extracts out the ``embedded_planning`` before saving the Event(s)
        And then uses them to synchronise/process the associated Planning item(s)
        """

        embedded_planning_lists: List[Tuple[Event, List[EmbeddedPlanning]]] = []
        for event in docs:
            embedded_planning = get_events_embedded_planning(event)
            if len(embedded_planning):
                embedded_planning_lists.append((event, embedded_planning))

        ids = self.backend.create(self.datasource, docs, **kwargs)

        if len(embedded_planning_lists):
            for event, embedded_planning in embedded_planning_lists:
                sync_event_metadata_with_planning_items(None, event, embedded_planning)

        return ids

    def validate_event(self, updates, original=None):
        """Validate the event

        @:param dict event: event created or updated
        """
        self._validate_multiday_event_duration(updates)
        self._validate_dates(updates, original)
        self._validate_convert_to_recurring(updates, original)
        self._validate_template(updates, original)

        # if len(updates.get('calendars', [])) > 0:
        # existing_calendars = get_resource_service('vocabularies').find_one(req=None, _id='event_calendars')
        # for calendar in updates['calendars']:
        # cal = [x for x in existing_calendars.get('items', []) if x['qcode'] == calendar.get('qcode')]
        # if not cal:
        # raise SuperdeskApiError(message="Calendar does not exist.")
        # if not cal[0].get('is_active'):
        # raise SuperdeskApiError(message="Disabled calendar cannot be selected.")

        # Remove duplicated calendars
        # uniq_qcodes = list_uniq_with_order([o['qcode'] for o in updates['calendars']])
        # updates['calendars'] = [cal for cal in existing_calendars.get('items', []) if cal['qcode'] in uniq_qcodes]
        sanitize_input_data(updates)

    def _validate_convert_to_recurring(self, updates, original):
        """Validates if the convert to recurring action is valid.

        :param updates:
        :param original:
        :return:
        """
        if not original:
            return

        if (
            original.get(LOCK_ACTION) == "convert_recurring"
            and updates.get("dates", {}).get("recurring_rule", None) is None
        ):
            raise SuperdeskApiError(message="Event recurring rules are mandatory for convert to recurring action.")
        if original.get(LOCK_ACTION) == "convert_recurring" and original.get("recurrence_id"):
            raise SuperdeskApiError(message="Event is already converted to recurring event.")

    def _validate_dates(self, updates, original=None):
        """Validate the dates

        @:param dict event:
        """
        event = updates if updates.get("dates") or not original else original
        start_date = event.get("dates", {}).get("start")
        end_date = event.get("dates", {}).get("end")

        if not start_date or not end_date:
            raise SuperdeskApiError(message="Event START DATE and END DATE are mandatory.")

        if end_date < start_date:
            raise SuperdeskApiError(message="END TIME should be after START TIME")

        if (
            event.get("dates", {}).get("recurring_rule")
            and not event["dates"]["recurring_rule"].get("until")
            and not event["dates"]["recurring_rule"].get("count")
        ):
            raise SuperdeskApiError(message="Recurring event should have an end (until or count)")

    def _validate_multiday_event_duration(self, event):
        """Validate that the multiday event duration is not greater than PLANNING_MAX_MULTI_DAY_DURATION

        @:param dict event: event created or updated
        """
        max_duration = get_event_max_multi_day_duration(app)
        if not max_duration > 0:
            return

        if not event.get("dates"):
            return

        event_duration = event.get("dates").get("end") - event.get("dates").get("start")
        if event_duration.days > max_duration:
            raise SuperdeskApiError(message="Event duration is greater than {} days.".format(max_duration))

    @staticmethod
    def _validate_template(updates, original):
        """Ensures that event template can't be changed

        :param updates: updates to event that should be saved
        :type updates: dict
        :param original: original event before update
        :type original: dict
        :return:
        """
        if not original:
            return

        # we can't change `template` id
        if "template" in updates and updates["template"] != original["template"]:
            raise SuperdeskApiError.badRequestError(
                message="Request is not valid",
                payload={"template": "This value can't be changed."},
            )

    def on_created(self, docs):
        """Send WebSocket Notifications for created Events

        Generate the list of IDs for recurring and non-recurring events
        Then send this list off to the clients so they can fetch these events
        """
        notifications_sent = []
        history_service = get_resource_service("events_history")

        for doc in docs:
            event_id = str(doc.get(config.ID_FIELD))
            # If we duplicated this event, update the history
            if doc.get("duplicate_from"):
                parent_id = doc["duplicate_from"]
                parent_event = self.find_one(req=None, _id=parent_id)

                history_service.on_item_updated({"duplicate_id": event_id}, parent_event, "duplicate")
                history_service.on_item_updated({"duplicate_id": parent_id}, doc, "duplicate_from")

                duplicate_ids = parent_event.get("duplicate_to", [])
                duplicate_ids.append(event_id)
                self.patch(parent_id, {"duplicate_to": duplicate_ids, config.ID_FIELD: parent_id})

            event_type = "events:created"
            user_id = str(doc.get("original_creator", ""))

            if doc.get("recurrence_id"):
                event_type = "events:created:recurring"
                event_id = str(doc["recurrence_id"])

            # Don't send notification if one has already been sent
            # This is to ensure recurring events doesn't send multiple notifications
            if event_id in notifications_sent or "previous_recurrence_id" in doc:
                continue

            notifications_sent.append(event_id)
            push_notification(event_type, item=event_id, user=user_id)

    @staticmethod
    def can_edit(item, user_id):
        # Check privileges
        if not current_user_has_privilege("planning_event_management"):
            return False, "User does not have sufficient permissions."
        return True, ""

    def update(self, id, updates, original):
        """Updated the Event in Mongo & Elastic

        Also extracts out the ``embedded_planning`` before saving the Event
        And then uses them to synchronise/process the associated Planning item(s)
        """

        updates.setdefault("versioncreated", utcnow())

        # Extract the ``embedded_planning`` from the updates
        embedded_planning = get_events_embedded_planning(updates)

        item = self.backend.update(self.datasource, id, updates, original)

        # Process ``embedded_planning`` field, and sync Event metadata with associated Planning/Coverages
        sync_event_metadata_with_planning_items(original, updates, embedded_planning)

        return item

    def on_update(self, updates, original):
        """Update single or series of recurring events.

        Determine if the supplied event is a single event or a
        series of recurring events, and call the appropriate method
        for the event type.
        """
        if "skip_on_update" in updates:
            # this is a recursive update (see below)
            del updates["skip_on_update"]
            return

        update_method = updates.pop("update_method", UPDATE_SINGLE)

        user = get_user()
        user_id = user.get(config.ID_FIELD) if user else None

        if user_id:
            updates["version_creator"] = user_id
            set_ingested_event_state(updates, original)

        lock_user = original.get("lock_user", None)
        str_user_id = str(user.get(config.ID_FIELD)) if user_id else None

        if lock_user and str(lock_user) != str_user_id:
            print(lock_user, str_user_id)
            raise SuperdeskApiError.forbiddenError("The item was locked by another user")

        # If only the `recurring_rule` was provided, then fill in the rest from the original
        # This can happen, for example, when converting a single Event to a series of Recurring Events
        if list(updates.get("dates") or {}) == ["recurring_rule"]:
            new_dates = deepcopy(original["dates"])
            new_dates.update(updates["dates"])
            updates["dates"] = new_dates

        # validate event
        self.validate_event(updates, original)

        # Run the specific methods based on if the original is a
        # single or a series of recurring events
        if not original.get("dates", {}).get("recurring_rule", None) or update_method == UPDATE_SINGLE:
            self._update_single_event(updates, original)
        else:
            self._update_recurring_events(updates, original, update_method)

    def on_updated(self, updates, original, from_ingest: Optional[bool] = None):
        # If this Event was converted to a recurring series
        # Then update all associated Planning items with the recurrence_id
        if updates.get("recurrence_id") and not original.get("recurrence_id"):
            get_resource_service("planning").on_event_converted_to_recurring(updates, original)

        if not updates.get("duplicate_to"):
            posted = update_post_item(updates, original)
            if posted:
                new_event = get_resource_service("events").find_one(req=None, _id=original.get(config.ID_FIELD))
                updates["_etag"] = new_event["_etag"]
                updates["state_reason"] = new_event.get("state_reason")

        if original.get("lock_user") and "lock_user" in updates and updates.get("lock_user") is None:
            # when the event is unlocked by the patch.
            push_notification(
                "events:unlock",
                item=str(original.get(config.ID_FIELD)),
                user=str(get_user_id()),
                lock_session=str(get_auth().get("_id")),
                etag=updates["_etag"],
                recurrence_id=original.get("recurrence_id") or None,
                from_ingest=from_ingest,
            )

        self.delete_event_files(updates, original)

        if "location" not in updates and original.get("location"):
            updates["location"] = original["location"]

        self._enhance_event_item(updates)

    def on_deleted(self, doc):
        push_notification(
            "events:delete",
            item=str(doc.get(config.ID_FIELD)),
            user=str(get_user_id()),
            lock_session=str(get_auth().get("_id")),
        )

    def _update_single_event(self, updates, original):
        """Updates the metadata of a single event.

        If recurring_rule is provided, we convert this single event into
        a series of recurring events, otherwise we simply update this event.
        """

        if post_required(updates, original):
            merged = deepcopy(original)
            merged.update(updates)
            get_resource_service("events_post").validate_item(merged)

        # Determine if we're to convert this single event to a recurring
        #  of events
        if (
            original.get(LOCK_ACTION) == "convert_recurring"
            and updates.get("dates", {}).get("recurring_rule", None) is not None
        ):
            generated_events = self._convert_to_recurring_event(updates, original)

            # if the original event was "posted" then post all the generated events
            if original.get("pubstatus") in [POST_STATE.CANCELLED, POST_STATE.USABLE]:
                post = {
                    "event": generated_events[0][config.ID_FIELD],
                    "etag": generated_events[0]["_etag"],
                    "update_method": "all",
                    "pubstatus": original.get("pubstatus"),
                }
                get_resource_service("events_post").post([post])

            push_notification(
                "events:updated:recurring",
                item=str(original[config.ID_FIELD]),
                user=str(updates.get("version_creator", "")),
                recurrence_id=str(generated_events[0]["recurrence_id"]),
            )
        else:
            if original.get("lock_action") == "mark_completed" and updates.get("actioned_date"):
                self.mark_event_complete(original, updates, original, None)

            # This updates Event metadata only
            push_notification(
                "events:updated",
                item=str(original[config.ID_FIELD]),
                user=str(updates.get("version_creator", "")),
            )

    def _update_recurring_events(self, updates, original, update_method):
        """Method to update recurring events.

        If the recurring_rule has been removed for this event, process
        it separately, otherwise update the event and/or its recurring rules
        """
        # This method now only handles updating of Event metadata
        # So make sure to remove any date information that might be in
        # the updates
        updates.pop("dates", None)

        if update_method == UPDATE_FUTURE:
            historic, past, future = self.get_recurring_timeline(original)
            events = future
        else:
            historic, past, future = self.get_recurring_timeline(original)
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
        only_calendars = original.get("lock_action") == "assign_calendar"
        original_calendar_qcodes = [calendar["qcode"] for calendar in original.get("calendars") or []]
        # Get the list of calendars added
        updated_calendars = [
            calendar for calendar in updates.get("calendars") or [] if calendar["qcode"] not in original_calendar_qcodes
        ]

        mark_completed = original.get("lock_action") == "mark_completed" and updates.get("actioned_date")
        mark_complete_validated = False
        for e in events:
            event_id = e[config.ID_FIELD]

            new_updates = deepcopy(updates)
            new_updates["skip_on_update"] = True
            new_updates[config.ID_FIELD] = event_id

            if only_calendars:
                # Get the original for this item, and add new calendars to it
                # Skipping calendars already assigned to this item
                original_event = self.find_one(req=None, _id=event_id)
                original_qcodes = [calendar["qcode"] for calendar in original_event.get("calendars") or []]

                new_updates["calendars"] = deepcopy(original_event.get("calendars") or [])
                new_updates["calendars"].extend(
                    [calendar for calendar in updated_calendars if calendar["qcode"] not in original_qcodes]
                )
            elif mark_completed:
                self.mark_event_complete(original, updates, e, mark_complete_validated)
                # It is validated if the previous funciton did not raise an error
                mark_complete_validated = True

            # Remove ``embedded_planning`` before updating this event, as this should only be handled
            # by the event provided to this update request
            new_updates.pop("embedded_planning", None)
            self.patch(event_id, new_updates)
            app.on_updated_events(new_updates, {"_id": event_id})

        # And finally push a notification to connected clients
        push_notification(
            "events:updated:recurring",
            item=str(original[config.ID_FIELD]),
            recurrence_id=str(original["recurrence_id"]),
            user=str(updates.get("version_creator", "")),
        )

    def mark_event_complete(self, original, updates, event, mark_complete_validated):
        # If the entire series is in future, raise an error
        if event.get("recurrence_id"):
            if not mark_complete_validated:
                if event["dates"]["start"].date() > updates["actioned_date"].date():
                    raise SuperdeskApiError.badRequestError("Recurring series has not started.")

            # If we are marking an event as completed
            # Update only those which are behind the 'actioned_date'
            if event["dates"]["start"] < updates["actioned_date"]:
                return

        plans = list(get_resource_service("planning").find(where={"event_item": event[config.ID_FIELD]}))
        for plan in plans:
            if plan.get("state") != WORKFLOW_STATE.CANCELLED and len(plan.get("coverages", [])) > 0:
                get_resource_service("planning_cancel").patch(
                    plan[config.ID_FIELD],
                    {
                        "reason": "Event Completed",
                        "cancel_all_coverage": True,
                    },
                )

    def _convert_to_recurring_event(self, updates, original):
        """Convert a single event to a series of recurring events"""
        self._validate_convert_to_recurring(updates, original)
        updates["recurrence_id"] = original["_id"]

        merged = copy.deepcopy(original)
        merged.update(updates)

        # Generated new events will be "draft"
        merged[ITEM_STATE] = WORKFLOW_STATE.DRAFT

        generated_events = generate_recurring_events(merged, updates["recurrence_id"])
        updated_event = generated_events.pop(0)

        # Check to see if the first generated event is different from original
        # If yes, mark original as rescheduled with generated recurrence_id
        if updated_event["dates"]["start"].date() != original["dates"]["start"].date():
            # Reschedule original event
            updates["update_method"] = UPDATE_SINGLE
            event_reschedule_service = get_resource_service("events_reschedule")
            updates["dates"] = updated_event["dates"]
            set_planning_schedule(updates)
            event_reschedule_service.update_single_event(updates, original)
            if updates.get("state") == WORKFLOW_STATE.RESCHEDULED:
                history_service = get_resource_service("events_history")
                history_service.on_reschedule(updates, original)
        else:
            # Original event falls as a part of the series
            # Remove the first element in the list (the current event being updated)
            # And update the start/end dates to be in line with the new recurring rules
            updates["dates"]["start"] = updated_event["dates"]["start"]
            updates["dates"]["end"] = updated_event["dates"]["end"]
            set_planning_schedule(updates)
            remove_lock_information(item=updates)

        # Create the new events and generate their history
        self.create(generated_events)
        app.on_inserted_events(generated_events)
        return generated_events

    def get_recurring_timeline(self, selected, spiked=False):
        events_base_service = EventsBaseService("events", backend=superdesk.get_backend())
        return events_base_service.get_recurring_timeline(selected, postponed=True, spiked=spiked)

    @staticmethod
    def _link_to_planning(event):
        """
        Links an Event to an existing Planning Item

        The Planning item remains locked, it is up to the client to release this lock
        after this operation is complete
        """
        planning_service = get_resource_service("planning")
        plan_id = event["_planning_item"]
        event_id = event[config.ID_FIELD]
        planning_item = planning_service.find_one(req=None, _id=plan_id)

        updates = {"event_item": event_id}

        if "recurrence_id" in event:
            updates["recurrence_id"] = event["recurrence_id"]

        planning_service.validate_on_update(updates, planning_item, get_user())

        planning_service.system_update(plan_id, updates, planning_item)
        app.on_updated_planning(updates, planning_item)

    def get_expired_items(self, expiry_datetime, spiked_events_only=False):
        """Get the expired items

        Where end date is in the past
        """
        query = {
            "query": {"bool": {"must_not": [{"term": {"expired": True}}]}},
            "filter": {"range": {"dates.end": {"lte": date_to_str(expiry_datetime)}}},
            "sort": [{"dates.start": "asc"}],
            "size": get_max_recurrent_events(),
        }

        if spiked_events_only:
            query["query"] = {"bool": {"must": [{"term": {"state": WORKFLOW_STATE.SPIKED}}]}}

        total_received = 0
        total_events = -1

        while total_received + get_max_recurrent_events() < 10000:  # 10k is max elastic limit
            query["from"] = total_received

            results = self.search(query)

            # If the total_events has not been set, then this is the first query
            # In which case we need to store the total hits from the search
            if total_events < 0:
                total_events = results.count()

                # If the search doesn't contain any results, return here
                if total_events < 1:
                    break

            # If the last query doesn't contain any results, return here
            if not len(results.docs):
                break

            total_received += len(results.docs)

            # Yield the results for iteration by the callee
            yield list(results.docs)

    def delete_event_files(self, updates, original):
        files = [f for f in original.get("files", []) if f not in (updates or {}).get("files", [])]
        files_service = get_resource_service("events_files")
        for file in files:
            events_using_file = self.find(where={"files": file})
            if events_using_file.count() == 0:
                files_service.delete_action(lookup={"_id": file})

    def should_update(self, old_item, new_item, provider):
        return old_item is None or not any(
            [
                old_item.get("pubstatus") == "cancelled",
                old_item.get("state") == "killed",
            ]
        )


class EventsResource(superdesk.Resource):
    """Resource for events data model

    See IPTC-G2-Implementation_Guide (version 2.21) Section 15.4 for schema details
    """

    endpoint_name = url = "events"
    schema = events_schema
    item_url = r'regex("[\w,.:-]+")'
    resource_methods = ["GET", "POST"]
    datasource = {
        "source": "events",
        "search_backend": "elastic",
        "default_sort": [("dates.start", 1)],
    }
    item_methods = ["GET", "PATCH"]
    mongo_indexes = {
        "recurrence_id_1": ([("recurrence_id", 1)], {"background": True}),
        "state": ([("state", 1)], {"background": True}),
        "dates_start_1": ([("dates.start", 1)], {"background": True}),
        "dates_end_1": ([("dates.end", 1)], {"background": True}),
        "template": [("template", 1)],
    }
    privileges = {
        "POST": "planning_event_management",
        "PATCH": "planning_event_management",
    }

    merge_nested_documents = True


def generate_recurring_dates(
    start,
    frequency,
    interval=1,
    endRepeatMode="count",
    until=None,
    byday=None,
    count=5,
    tz=None,
    date_only=False,
    _created_externally=False,
):
    """

    Returns list of dates related to recurring rules

    :param start datetime: date when to start
    :param frequency str: DAILY, WEEKLY, MONTHLY, YEARLY
    :param interval int: indicates how often the rule repeats as a positive integer
    :param until datetime: date after which the recurrence rule expires
    :param byday str or list: "MO TU"
    :param count int: number of occurrences of the rule
    :return list: list of datetime

    """
    # if tz is given, respect the timzone by starting from the local time
    # NOTE: rrule uses only naive datetime
    if tz:
        try:
            # start can already be localized
            start = pytz.UTC.localize(start)
        except ValueError:
            pass
        start = start.astimezone(tz).replace(tzinfo=None)
        if until:
            until = get_date(until).astimezone(tz).replace(tzinfo=None)

    if frequency == "DAILY":
        byday = None

    # check format of the recurring_rule byday value
    if byday and re.match(r"^-?[1-5]+.*", byday):
        # byday uses monthly or yearly frequency rule with day of week and
        # preceding day of month integer by day value
        # examples:
        # 1FR - first friday of the month
        # -2MON - second to last monday of the month
        if byday[:1] == "-":
            day_of_month = int(byday[:2])
            day_of_week = byday[2:]
        else:
            day_of_month = int(byday[:1])
            day_of_week = byday[1:]

        byweekday = DAYS.get(day_of_week)(day_of_month)
    else:
        # byday uses DAYS constants
        byweekday = byday and [DAYS.get(d) for d in byday.split()] or None

    # Convert count of repeats to count of events
    if count:
        count = count * (len(byday.split()) if byday else 1)

    # TODO: use dateutil.rrule.rruleset to incude ex_date and ex_rule
    dates = rrule(
        FREQUENCIES.get(frequency),
        dtstart=start,
        until=until,
        byweekday=byweekday,
        count=count,
        interval=interval,
    )
    # if a timezone has been applied, returns UTC
    if tz:
        if date_only:
            return (tz.localize(dt).astimezone(pytz.UTC).replace(tzinfo=None).date() for dt in dates)
        else:
            return (tz.localize(dt).astimezone(pytz.UTC).replace(tzinfo=None) for dt in dates)
    else:
        if date_only:
            return (date.date() for date in dates)
        else:
            return (date for date in dates)


def setRecurringMode(event):
    endRepeatMode = event.get("dates", {}).get("recurring_rule", {}).get("endRepeatMode")
    if endRepeatMode == "count":
        event["dates"]["recurring_rule"]["until"] = None
    elif endRepeatMode == "until":
        event["dates"]["recurring_rule"]["count"] = None


def overwrite_event_expiry_date(event):
    if "expiry" in event:
        expiry_minutes = app.settings.get("PLANNING_EXPIRY_MINUTES", None)
        event["expiry"] = event["dates"]["end"] + timedelta(minutes=expiry_minutes or 0)


def generate_recurring_events(event, recurrence_id=None):
    generated_events = []
    setRecurringMode(event)
    embedded_planning_added = False

    # compute the difference between start and end in the original event
    time_delta = event["dates"]["end"] - event["dates"]["start"]
    # for all the dates based on the recurring rules:
    for date in itertools.islice(
        generate_recurring_dates(
            start=event["dates"]["start"],
            tz=event["dates"].get("tz") and pytz.timezone(event["dates"]["tz"] or None),
            **event["dates"]["recurring_rule"],
        ),
        0,
        get_max_recurrent_events(),
    ):  # set a limit to prevent too many events to be created
        # create event with the new dates
        new_event = copy.deepcopy(event)

        # Remove fields not required by the new events
        for key in list(new_event.keys()):
            if key.startswith("_") or key.startswith("lock_"):
                new_event.pop(key)
            elif key == "embedded_planning":
                if not embedded_planning_added:
                    # If this is the first Event in the series, then keep
                    # the ``embedded_planning`` field for processing later
                    embedded_planning_added = True
                else:
                    # Otherwise remove the ``embedded_planning`` from all other Events
                    # in the series
                    new_event.pop("embedded_planning")

        new_event.pop("pubstatus", None)
        new_event.pop("reschedule_from", None)

        new_event["dates"]["start"] = date
        new_event["dates"]["end"] = date + time_delta
        # set a unique guid
        new_event["guid"] = generate_guid(type=GUID_NEWSML)
        new_event["_id"] = new_event["guid"]
        # set the recurrence id
        if not recurrence_id:
            recurrence_id = new_event["guid"]
        new_event["recurrence_id"] = recurrence_id

        # set expiry date
        overwrite_event_expiry_date(new_event)
        # the _planning_schedule
        set_planning_schedule(new_event)
        generated_events.append(new_event)

    return generated_events


def set_planning_schedule(event):
    if event and event.get("dates") and event["dates"].get("start"):
        event["_planning_schedule"] = [{"scheduled": event["dates"]["start"]}]
