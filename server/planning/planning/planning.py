# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
#  Copyright 2013, 2014 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Superdesk Planning"""
from typing import Dict, Any, Optional, List
from bson import ObjectId
from copy import deepcopy
import logging
from datetime import datetime

from flask import json, current_app as app, request
from eve.methods.common import resolve_document_etag

import superdesk
from superdesk.errors import SuperdeskApiError
from planning.errors import AssignmentApiError

from superdesk.metadata.utils import generate_guid, item_url
from superdesk.metadata.item import GUID_NEWSML, metadata_schema, ITEM_TYPE, CONTENT_STATE
from superdesk import get_resource_service
from superdesk.resource import not_analyzed, string_with_analyzer
from superdesk.users.services import current_user_has_privilege
from superdesk.notification import push_notification

from apps.archive.common import get_user, get_auth, update_dates_for
from eve.utils import config, ParsedRequest, date_to_str
from planning.types import Planning, Coverage, Event, UPDATE_METHOD
from planning.common import (
    WORKFLOW_STATE_SCHEMA,
    POST_STATE_SCHEMA,
    get_coverage_status_from_cv,
    WORKFLOW_STATE,
    ASSIGNMENT_WORKFLOW_STATE,
    prepare_ingested_item_for_storage,
    update_post_item,
    get_coverage_type_name,
    set_original_creator,
    list_uniq_with_order,
    TEMP_ID_PREFIX,
    DEFAULT_ASSIGNMENT_PRIORITY,
    get_planning_allow_scheduled_updates,
    TO_BE_CONFIRMED_FIELD,
    TO_BE_CONFIRMED_FIELD_SCHEMA,
    get_planning_xmp_assignment_mapping,
    sanitize_input_data,
    get_planning_xmp_slugline_mapping,
    get_planning_use_xmp_for_pic_slugline,
    get_planning_use_xmp_for_pic_assignments,
    sync_assignment_details_to_coverages,
    set_ingest_version_datetime,
    is_new_version,
    update_ingest_on_patch,
    UPDATE_METHODS,
    UPDATE_SINGLE,
    UPDATE_FUTURE,
    UPDATE_ALL,
    POST_STATE,
)
from superdesk.utc import utcnow
from itertools import chain
from planning.planning_notifications import PlanningNotifications
from superdesk.utc import utc_to_local
from planning.content_profiles.utils import is_field_enabled, is_post_planning_with_event_enabled
from superdesk import Resource
from lxml import etree
from io import BytesIO
from planning.signals import planning_created, planning_ingested

logger = logging.getLogger(__name__)


def get_coverage_by_id(
    planning_item: Planning, coverage_id: str, field: Optional[str] = "coverage_id"
) -> Optional[Coverage]:
    return next(
        (coverage for coverage in planning_item.get("coverages") or [] if coverage.get(field) == coverage_id),
        None,
    )


class PlanningService(superdesk.Service):
    """Service class for the planning model."""

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
        for doc in docs:
            planning_ingested.send(self, item=doc)
        return ids

    def patch_in_mongo(self, id, document, original):
        """Patch an ingested item onto an existing item locally"""
        prepare_ingested_item_for_storage(document)
        update_ingest_on_patch(document, original)
        response = self.backend.update_in_mongo(self.datasource, id, document, original)
        self.on_updated(document, original, from_ingest=True)
        planning_ingested.send(self, item=document, original=original)
        return response

    def is_new_version(self, new_item, old_item):
        return is_new_version(new_item, old_item)

    def ingest_cancel(self, item, feeding_service):
        """Ignore cancelling on ingest, this will happen in ``update_post_item``"""

        pass

    def generate_related_assignments(self, docs):
        for doc in docs:
            doc.pop("_planning_schedule", None)
            doc.pop("_updates_schedule", None)
            sync_assignment_details_to_coverages(doc)

    def on_fetched(self, docs):
        self.generate_related_assignments(docs.get(config.ITEMS))

    def on_fetched_item(self, doc):
        self.generate_related_assignments([doc])

    def find_one(self, req, **lookup):
        item = super().find_one(req, **lookup)
        if item:
            self.generate_related_assignments([item])
            for coverage in item.get("coverages", []):
                if coverage.get("planning", {}).get("scheduled") and not isinstance(
                    coverage["planning"]["scheduled"], datetime
                ):
                    coverage["planning"]["scheduled"] = datetime.strptime(
                        coverage["planning"]["scheduled"], "%Y-%m-%dT%H:%M:%S%z"
                    )
        return item

    def on_create(self, docs):
        """Set default metadata."""
        planning_type = get_resource_service("planning_types").find_one(req=None, name="planning")
        history_service = get_resource_service("planning_history")
        generated_planning_items = []
        for doc in docs:
            if "guid" not in doc:
                doc["guid"] = generate_guid(type=GUID_NEWSML)
            doc[config.ID_FIELD] = doc["guid"]

            # SDCP-638
            if not doc.get("language"):
                try:
                    doc["language"] = doc["languages"][0]
                except (KeyError, IndexError):
                    doc["language"] = app.config["DEFAULT_LANGUAGE"]

            self.validate_planning(doc)
            set_original_creator(doc)
            event: Event = self._set_planning_event_info(doc, planning_type)
            self._set_coverage(doc)
            self.set_planning_schedule(doc)
            # set timestamps
            update_dates_for(doc)

            is_ingested = doc["state"] == "ingested"
            if is_ingested:
                history_service.on_item_created([doc])

            update_method: Optional[UPDATE_METHOD] = doc.pop("update_method", None)
            if event and update_method is not None:
                new_plans = self._add_planning_to_event_series(doc, event, update_method)
                if len(new_plans):
                    if is_ingested:
                        history_service.on_item_created(new_plans)
                    generated_planning_items.extend(new_plans)

        if len(generated_planning_items):
            docs.extend(generated_planning_items)

    def on_created(self, docs):
        session_id = get_auth().get("_id")
        post_planning_with_event = is_post_planning_with_event_enabled()
        for doc in docs:
            plan_id = str(doc.get(config.ID_FIELD))
            push_notification(
                "planning:created",
                item=plan_id,
                user=str(doc.get("original_creator", "")),
                added_agendas=doc.get("agendas") or [],
                removed_agendas=[],
                session=session_id,
                event_item=doc.get("event_item", None),
            )
            self._update_event_history(doc)
            planning_created.send(self, item=doc)

            event_id = doc.get("event_item")
            if event_id and post_planning_with_event:
                event = get_resource_service("events").find_one(req=None, _id=event_id)
                if not event:
                    logger.warning(f"Failed to find linked event {event_id} for planning {plan_id}")
                elif event.get("pubstatus") == POST_STATE.USABLE:
                    updates = doc.copy()
                    updates["pubstatus"] = POST_STATE.USABLE
                    update_post_item(updates, doc)

        self.generate_related_assignments(docs)

    def _update_event_history(self, doc):
        event_id = doc.get("event_item")
        if not event_id:
            return
        events_service = get_resource_service("events")
        original_event = events_service.find_one(req=None, _id=event_id)

        if not original_event:
            logger.warning(f"Failed to update event history, Event '{event_id}' not found")
            return

        events_service.system_update(
            doc["event_item"],
            {
                "expiry": None,
                # Event hasn't actually been updated
                # So we leave these version dates alone
                "_updated": original_event["_updated"],
                "versioncreated": original_event["versioncreated"],
            },
            original_event,
        )

        get_resource_service("events_history").on_item_updated(
            {"planning_id": doc.get("_id")}, original_event, "planning_created"
        )

    def on_duplicated(self, doc, parent_id):
        self._update_event_history(doc)
        session_id = get_auth().get("_id")
        push_notification(
            "planning:duplicated",
            item=str(doc.get(config.ID_FIELD)),
            original=str(parent_id),
            user=str(doc.get("original_creator", "")),
            added_agendas=doc.get("agendas") or [],
            removed_agendas=[],
            session=session_id,
        )

    def on_locked_planning(self, item, user_id):
        self.generate_related_assignments([item])

    def should_update(self, old_item, new_item, provider):
        return True

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

    def update(self, id, updates, original):
        updates.setdefault("versioncreated", utcnow())
        item = self.backend.update(self.datasource, id, updates, original)
        return item

    def on_update(self, updates, original):
        update_method = updates.pop("update_method", UPDATE_SINGLE)
        user = get_user()
        self.validate_on_update(updates, original, user)

        if user and user.get(config.ID_FIELD):
            updates["version_creator"] = user[config.ID_FIELD]

        self._set_coverage(updates, original)
        self.set_planning_schedule(updates, original)

        if update_method and update_method != UPDATE_SINGLE:
            self._update_recurring_planning_items(updates, original, update_method)

    def validate_on_update(self, updates, original, user):
        lock_user = original.get("lock_user", None)
        str_user_id = str(user.get(config.ID_FIELD)) if user else None

        if lock_user and str(lock_user) != str_user_id:
            raise SuperdeskApiError.forbiddenError("The item was locked by another user")

        self.validate_planning(updates, original)

    def validate_planning(self, updates, original=None):
        if (not original and not updates.get("planning_date")) or (
            "planning_date" in updates and updates["planning_date"] is None
        ):
            raise SuperdeskApiError(message="Planning item should have a date")

        sanitize_input_data(updates)

        # Validate if agendas being added are enabled agendas
        agenda_service = get_resource_service("agenda")
        for agenda_id in updates.get("agendas", []):
            agenda = agenda_service.find_one(req=None, _id=agenda_id)
            if not agenda:
                raise SuperdeskApiError.forbiddenError("Agenda '{}' does not exist".format(agenda_id))

            if not agenda.get("is_enabled", False) and (
                original is None or agenda_id not in original.get("agendas", [])
            ):
                raise SuperdeskApiError.forbiddenError("Agenda '{}' is not enabled".format(agenda.get("name")))

        # Remove duplicate agendas
        if len(updates.get("agendas", [])) > 0:
            updates["agendas"] = list_uniq_with_order(updates["agendas"])

        # Validate scheduled updates
        for coverage in updates.get("coverages") or []:
            coverage_schedule = (coverage.get("planning") or {}).get("scheduled")
            schedule_updates = list(coverage.get("scheduled_updates") or [])
            schedule_updates.reverse()
            for i, scheduled_update in enumerate(schedule_updates):
                scheduled_update_schedule = (scheduled_update.get("planning") or {}).get("scheduled")
                if not scheduled_update_schedule:
                    continue

                if coverage_schedule and scheduled_update_schedule <= coverage_schedule:
                    raise SuperdeskApiError(message="Scheduled updates must be after the original coverage.")

                next_schedule = next(
                    (
                        s
                        for s in schedule_updates[i + 1 : len(schedule_updates)]
                        if (s.get("planning") or {}).get("scheduled") is not None
                    ),
                    None,
                )
                if next_schedule and next_schedule["planning"]["scheduled"] > scheduled_update["planning"]["scheduled"]:
                    raise SuperdeskApiError(message="Scheduled updates of a coverage must be after the previous update")

    def _set_planning_event_info(self, doc, planning_type) -> Optional[Dict[str, Any]]:
        """Set the planning event date

        :param dict doc: planning document
        :param dict planning_types: planning type
        """
        event_id = doc.get("event_item")

        if not event_id:
            return None

        event = get_resource_service("events").find_one(req=None, _id=event_id)

        if not event:
            plan_id = doc.get("_id")
            logger.warning(f"Failed to find linked event {event_id} for planning {plan_id}")
            return None

        if event.get("recurrence_id"):
            doc["recurrence_id"] = event.get("recurrence_id")

        # populate headline using name
        if event.get("name") and is_field_enabled("headline", planning_type):
            doc.setdefault("headline", event["name"])

        if event.get(TO_BE_CONFIRMED_FIELD):
            doc[TO_BE_CONFIRMED_FIELD] = True

        return event

    def _add_planning_to_event_series(
        self, plan: Planning, event: Event, update_method: UPDATE_METHOD
    ) -> List[Dict[str, Any]]:
        if update_method not in [UPDATE_FUTURE, UPDATE_ALL]:
            return []

        recurrence_id = event.get("recurrence_id")
        if not recurrence_id:
            # Not a series of Events, can safely return
            return []

        plan["planning_recurrence_id"] = generate_guid(type=GUID_NEWSML)
        planning_date_relative = plan["planning_date"] - event["dates"]["start"]
        items = []

        events_service = get_resource_service("events")
        historic, past, future = events_service.get_recurring_timeline(event)
        event_series = future if update_method == UPDATE_FUTURE else historic + past + future

        for series_entry in event_series:
            if series_entry["_id"] == event["_id"]:
                # This is the Event that was provided
                # We assume a Planning item was already created for this Event
                continue

            new_plan = deepcopy(plan)

            # Set the Planning & Event IDs for the new item
            new_plan["guid"] = new_plan["_id"] = generate_guid(type=GUID_NEWSML)
            new_plan["event_item"] = series_entry["_id"]
            new_plan["recurrence_id"] = recurrence_id

            # Set the Planning date/time relative to the Event start date/time
            new_plan["planning_date"] = series_entry["dates"]["start"] + planning_date_relative
            for coverage in new_plan.get("coverages") or []:
                # Remove the Coverage and Assignment IDs (as these will be created for us in ``self._set_coverage``)
                coverage["original_coverage_id"] = coverage.pop("coverage_id", None)
                (coverage.get("assigned_to") or {}).pop("assignment_id", None)

                # Set the scheduled date/time relative to the Event start date/time
                coverage_date_relative = coverage["planning"]["scheduled"] - event["dates"]["start"]
                coverage["planning"]["scheduled"] = series_entry["dates"]["start"] + coverage_date_relative

            self._set_coverage(new_plan)
            self.set_planning_schedule(new_plan)

            items.append(new_plan)

        return items

    def _get_added_removed_agendas(self, updates, original):
        updated_agendas = [str(a) for a in (updates.get("agendas") or [])]
        existing_agendas = [str(a) for a in (original.get("agendas") or [])]
        removed_agendas = list(set(existing_agendas) - set(updated_agendas))
        added_agendas = list(set(updated_agendas) - set(existing_agendas))
        return added_agendas, removed_agendas

    def on_updated(self, updates, original, from_ingest=False):
        added, removed = self._get_added_removed_agendas(updates, original)
        item_id = str(original[config.ID_FIELD])
        session_id = get_auth().get(config.ID_FIELD)
        user_id = str(updates.get("version_creator", ""))
        push_notification(
            "planning:updated",
            item=item_id,
            user=str(updates.get("version_creator", "")),
            added_agendas=added,
            removed_agendas=removed,
            session=session_id,
        )

        doc = deepcopy(original)
        doc.update(updates)
        self.generate_related_assignments([doc])
        updates["coverages"] = doc.get("coverages") or []

        if original.get("lock_user") and "lock_user" in updates and updates.get("lock_user") is None:
            # When the Planning is unlocked by a patch
            push_notification(
                "planning:unlock",
                item=item_id,
                user=user_id,
                lock_session=session_id,
                etag=updates["_etag"],
                event_item=original.get("event_item"),
                recurrence_id=original.get("recurrence_id") or None,
                from_ingest=from_ingest,
            )

        posted = update_post_item(updates, original)
        if posted:
            new_planning = self.find_one(req=None, _id=original.get(config.ID_FIELD))
            updates["_etag"] = new_planning["_etag"]

    def can_edit(self, item, user_id):
        # Check privileges
        if not current_user_has_privilege("planning_planning_management"):
            return False, "User does not have sufficient permissions."
        return True, ""

    def get_planning_by_agenda_id(self, agenda_id):
        """Get the planing item by Agenda

        :param dict agenda_id: Agenda _id
        :return list: list of planing items
        """
        query = {"query": {"bool": {"must": {"term": {"agendas": str(agenda_id)}}}}}
        req = ParsedRequest()
        req.args = {"source": json.dumps(query)}
        return super().get(req=req, lookup=None)

    def get_all_items_in_relationship(self, item):
        all_items = []
        if item.get("event_item"):
            if item.get("recurrence_id"):
                event_param = {
                    "_id": item.get("event_item"),
                    "recurrence_id": item.get("recurrence_id"),
                }
                # One call wil get all items in the recurring series from event service
                return get_resource_service("events").get_all_items_in_relationship(event_param)
            else:
                event_param = {"_id": item.get("event_item")}
                # Get associated event
                all_items = get_resource_service("events").find(where={"_id": item.get("event_item")})
                # Get all associated planning items
                return chain(
                    all_items,
                    get_resource_service("events").get_plannings_for_event(event_param),
                )
        else:
            return all_items

    def remove_coverages(self, updates, original):
        if "coverages" not in updates:
            return

        for coverage in (original or {}).get("coverages") or []:
            updated_coverage = next(
                (
                    cov
                    for cov in updates.get("coverages") or []
                    if cov.get("coverage_id") == coverage.get("coverage_id")
                ),
                None,
            )

            if not updated_coverage:
                for s in coverage.get("scheduled_updates") or []:
                    self.remove_coverage_entity(s, original)

                self.remove_coverage_entity(coverage, original)

    def set_coverage_active(self, coverage, planning, parentCoverage=None):
        # If the coverage is created and assigned to a desk/user and the PLANNING_AUTO_ASSIGN_TO_WORKFLOW is
        # True the coverage will be created in workflow unless the overide flag is set.
        if (
            app.config.get("PLANNING_AUTO_ASSIGN_TO_WORKFLOW", False)
            and (coverage.get("assigned_to", {}).get("desk") or coverage.get("assigned_to", {}).get("user"))
            and not planning.get("flags", {}).get("overide_auto_assign_to_workflow", False)
            and coverage["workflow_status"] == WORKFLOW_STATE.DRAFT
        ):
            coverage["workflow_status"] = WORKFLOW_STATE.ACTIVE

            # set all scheduled_updates to be activated
            for s in coverage.get("scheduled_updates") or []:
                if s.get("assigned_to") and s["workflow_status"] == WORKFLOW_STATE.DRAFT:
                    s["workflow_status"] = WORKFLOW_STATE.ACTIVE

            return

        assigned_to = coverage.get("assigned_to")
        if (assigned_to and assigned_to.get("state") == ASSIGNMENT_WORKFLOW_STATE.ASSIGNED) or (
            parentCoverage or {}
        ).get("workflow_status") == WORKFLOW_STATE.ACTIVE:
            coverage["workflow_status"] = WORKFLOW_STATE.ACTIVE
            return

    def remove_coverage_entity(self, coverage_entity, original_planning, entity_type="coverage"):
        if original_planning.get("state") == WORKFLOW_STATE.CANCELLED:
            raise SuperdeskApiError.badRequestError("Cannot remove {} of a cancelled planning item".format(entity_type))

        assignment = coverage_entity.get("assigned_to", None)
        if assignment and assignment.get("state") not in [
            WORKFLOW_STATE.DRAFT,
            WORKFLOW_STATE.CANCELLED,
            None,
        ]:
            raise SuperdeskApiError.badRequestError(
                "Assignment already exists. {} cannot be deleted.".format(entity_type.capitalize())
            )

        updated_coverage_entity = deepcopy(coverage_entity)
        updated_coverage_entity.pop("assigned_to", None)
        self._create_update_assignment(original_planning, {}, updated_coverage_entity, coverage_entity)

    def add_coverages(self, updates, original):
        if "coverages" not in updates:
            return

        planning_date = original.get("planning_date") or updates.get("planning_date")
        original_coverage_ids = [
            coverage["coverage_id"] for coverage in original.get("coverages") or [] if coverage.get("coverage_id")
        ]
        for coverage in updates.get("coverages") or []:
            coverage_id = coverage.get("coverage_id", "")
            if not coverage_id or TEMP_ID_PREFIX in coverage_id or coverage_id not in original_coverage_ids:
                if "duplicate" in coverage_id or coverage.get("original_coverage_id"):
                    self.duplicate_xmp_file(coverage)
                # coverage to be created
                if not coverage_id or TEMP_ID_PREFIX in coverage_id:
                    coverage["coverage_id"] = generate_guid(type=GUID_NEWSML)
                if coverage.get("original_coverage_id") is None:
                    coverage["original_coverage_id"] = coverage["coverage_id"]
                coverage["firstcreated"] = utcnow()

                # Make sure the coverage has a ``scheduled`` date
                # If none was supplied, fallback to to ``planning.planning_date``
                coverage.setdefault("planning", {})
                coverage["planning"].setdefault("scheduled", planning_date)

                set_original_creator(coverage)
                self.set_coverage_active(coverage, updates)
                self.set_slugline_from_xmp(coverage, None)
                self._create_update_assignment(original, updates, coverage)
                self.add_scheduled_updates(updates, original, coverage)

    def set_scheduled_update_active(self, scheduled_update, planning, coverage):
        self.set_coverage_active(scheduled_update, planning, coverage)

        if (
            coverage.get("workflow_status") == WORKFLOW_STATE.DRAFT
            and scheduled_update.get("workflow_status") == WORKFLOW_STATE.ACTIVE
        ):
            raise SuperdeskApiError(
                message="Cannot add a scheduled update to workflow when original coverage is not in workflow"
            )

    def remove_scheduled_updates(self, updates, original, coverage, original_coverage):
        for s in original_coverage.get("scheduled_updates") or []:
            updated_s = next(
                (
                    updated_s
                    for updated_s in coverage.get("scheduled_updates") or []
                    if updated_s.get("scheduled_update_id") == s.get("scheduled_update_id")
                ),
                None,
            )

            if not updated_s:
                self.remove_coverage_entity(s, original)

    def add_scheduled_updates(self, updates, original, coverage):
        for s in coverage.get("scheduled_updates") or []:
            if not get_planning_allow_scheduled_updates():
                raise SuperdeskApiError(message="Not configured to create scheduled updates to a coverage")

            if not s.get("scheduled_update_id") or TEMP_ID_PREFIX in s["scheduled_update_id"]:
                s["coverage_id"] = coverage["coverage_id"]
                s["scheduled_update_id"] = generate_guid(type=GUID_NEWSML)
                self.set_scheduled_update_active(s, updates, coverage)
                self._create_update_assignment(original, updates, s, None, coverage)

    def update_scheduled_updates(self, updates, original, coverage, original_coverage):
        for s in coverage.get("scheduled_updates") or []:
            original_scheduled_update = next(
                (
                    orig_s
                    for orig_s in (original_coverage.get("scheduled_updates") or [])
                    if s["scheduled_update_id"] == orig_s.get("scheduled_update_id")
                ),
                None,
            )

            if original_scheduled_update:
                if (
                    original_scheduled_update.get("workflow_status") == WORKFLOW_STATE.DRAFT
                    and s.get("workflow_status") == WORKFLOW_STATE.ACTIVE
                ):
                    self.set_scheduled_update_active(s, updates, coverage)
                self._create_update_assignment(original, updates, s, original_scheduled_update, coverage)

    def update_coverages(self, updates, original):
        if "coverages" not in updates:
            return

        for coverage in updates.get("coverages") or []:
            coverage_id = coverage.get("coverage_id")
            original_coverage = next(
                (cov for cov in original.get("coverages") or [] if cov["coverage_id"] == coverage_id),
                None,
            )
            if not original_coverage:
                continue

            if (original_coverage.get("flags") or {}).get("no_content_linking") != (coverage.get("flags") or {}).get(
                "no_content_linking"
            ) and coverage.get("workflow_status") != WORKFLOW_STATE.DRAFT:
                raise SuperdeskApiError.badRequestError(
                    "Cannot edit content linking flag of a coverage already in workflow"
                )

            # Make sure the coverage update has a ``scheduled`` date
            # If none was supplied, fallback to ``original.planning.scheduled``
            coverage.setdefault("planning", {})
            coverage["planning"].setdefault("scheduled", (original_coverage.get("planning") or {}).get("scheduled"))
            self.set_coverage_active(coverage, updates)
            self.set_slugline_from_xmp(coverage, original_coverage)
            if self.coverage_changed(coverage, original_coverage):
                user = get_user()
                if user:
                    # ``version_creator`` cannot be null
                    coverage["version_creator"] = str(user.get(config.ID_FIELD))
                coverage["versioncreated"] = utcnow()

                contact_id = coverage.get(
                    "contact",
                    (original_coverage.get("assigned_to") or {}).get("contact", None),
                )

                # If the internal note has changed send a notification, except if it's been cancelled
                if (
                    coverage.get("planning", {}).get("internal_note", "")
                    != original_coverage.get("planning", {}).get("internal_note", "")
                    and coverage.get("news_coverage_status", {}).get("qcode") != "ncostat:notint"
                ):
                    target_user = coverage.get("assigned_to", original_coverage.get("assigned_to", {})).get(
                        "user", None
                    )
                    target_desk = coverage.get("assigned_to", original_coverage.get("assigned_to", {})).get(
                        "desk", None
                    )

                    PlanningNotifications().notify_assignment(
                        coverage_status=coverage.get("workflow_status"),
                        target_desk=target_desk if target_user is None else None,
                        target_user=target_user,
                        contact_id=contact_id,
                        message="assignment_internal_note_msg",
                        coverage_type=get_coverage_type_name(coverage.get("planning", {}).get("g2_content_type", "")),
                        slugline=coverage.get("planning", {}).get("slugline", ""),
                        internal_note=coverage.get("planning", {}).get("internal_note", ""),
                    )
                # If the scheduled time for the coverage changes
                if coverage.get("planning", {}).get("scheduled", datetime.min).strftime("%c") != original_coverage.get(
                    "planning", {}
                ).get("scheduled", datetime.min).strftime("%c"):
                    target_user = coverage.get("assigned_to", original_coverage.get("assigned_to", {})).get(
                        "user", None
                    )
                    target_desk = coverage.get("assigned_to", original_coverage.get("assigned_to", {})).get(
                        "desk", None
                    )
                    PlanningNotifications().notify_assignment(
                        coverage_status=coverage.get("workflow_status"),
                        target_desk=target_desk if target_user is None else None,
                        target_user=target_user,
                        contact_id=contact_id,
                        message="assignment_due_time_msg",
                        due=utc_to_local(
                            app.config["DEFAULT_TIMEZONE"],
                            coverage.get("planning", {}).get("scheduled"),
                        ).strftime("%c"),
                        coverage_type=get_coverage_type_name(coverage.get("planning", {}).get("g2_content_type", "")),
                        slugline=coverage.get("planning", {}).get("slugline", ""),
                    )

            self.add_scheduled_updates(updates, original, coverage)
            self.update_scheduled_updates(updates, original, coverage, original_coverage)
            self.remove_scheduled_updates(updates, original, coverage, original_coverage)

            self._create_update_assignment(original, updates, coverage, original_coverage)

    def _set_coverage(self, updates, original=None):
        if "coverages" not in updates:
            return

        if not original:
            original = {}

        # [SDESK-3073]: Commenting the following section as we cannot reproduce the ******
        # scenario where a patch is sent without any coverages (unless all coverages are removed)
        # if not updates.get('coverages'):
        # # If the description text has changed, make sure to update the assignment(s)
        # if updates.get('description_text') or updates.get('internal_note'):
        # for coverage in (original.get('coverages') or []):
        # self._create_update_assignment(original, updates, coverage, coverage)
        # return
        # ********* [SDESK-3073]: End revert ***************"""

        self.remove_coverages(updates, original)
        self.add_coverages(updates, original)
        self.update_coverages(updates, original)

    @staticmethod
    def coverage_changed(updates, original):
        for field in ["news_coverage_status", "planning", "workflow_status"]:
            if updates.get(field) != original.get(field):
                return True

        return False

    def set_planning_schedule(self, updates, original=None):
        """This set the list of schedule based on the coverage and planning.

        Sorting currently works on two fields "planning_date" and "scheduled" date.
        "planning_date" is stored on the planning and is equal to event start date for planning items
        created from event or current date for adhoc planning item
        "scheduled" is stored on the coverage nested document and it is optional.
        Hence to sort and filter planning based on these two dates a
        nested documents of scheduled date is required

        :param dict updates: planning update document
        :param dict original: planning original document
        """

        coverages = updates.get("coverages", [])
        planning_date = updates.get("planning_date") or (original or {}).get("planning_date") or utcnow()

        add_default_schedule = True
        add_default_updates_schedule = True
        schedule = []
        updates_schedule = []
        for coverage in coverages:
            if coverage.get("planning", {}).get("scheduled"):
                add_default_schedule = False

            schedule.append(
                {
                    "coverage_id": coverage.get("coverage_id"),
                    "scheduled": coverage.get("planning", {}).get("scheduled"),
                }
            )

            for s in coverage.get("scheduled_updates") or []:
                if s.get("planning", {}).get("scheduled") and add_default_updates_schedule:
                    add_default_updates_schedule = False

                updates_schedule.append(
                    {
                        "scheduled_update_id": s.get("scheduled_update_id"),
                        "scheduled": s.get("planning", {}).get("scheduled"),
                    }
                )

        if add_default_schedule:
            schedule.append({"coverage_id": None, "scheduled": planning_date or utcnow()})

        if add_default_updates_schedule:
            updates_schedule.append({"scheduled_update_id": None, "scheduled": planning_date or utcnow()})

        updates["_planning_schedule"] = schedule
        updates["_updates_schedule"] = updates_schedule

    def _create_update_assignment(
        self,
        planning_original,
        planning_updates,
        updates,
        original=None,
        parent_coverage=None,
    ):
        """Create or update the assignment.

        :param dict planning_original: original parent planning document
        :param dict planning_updates: updates for the parent planning document
        :param dict updates: coverage update document
        :param dict original: coverage original document
        """
        if not original:
            original = {}

        planning = deepcopy(planning_original)
        planning.update(planning_updates)
        planning_id = planning.get(config.ID_FIELD)

        doc = deepcopy(original)
        doc.update(deepcopy(updates))
        assignment_service = get_resource_service("assignments")
        assigned_to = updates.get("assigned_to") or original.get("assigned_to")
        new_assignment_id = None
        if not assigned_to:
            return

        if not planning_id:
            raise SuperdeskApiError.badRequestError("Planning item is required to create assignments.")

        # Coverage is draft if original was draft and updates is still maintaining that state
        coverage_status = updates.get("workflow_status", original.get("workflow_status"))
        is_coverage_draft = coverage_status == WORKFLOW_STATE.DRAFT

        translations = planning.get("translations")
        translated_value = {}
        translated_name = planning.get("name", planning.get("headline", ""))
        doc.setdefault("planning", {})
        if translations is not None and doc["planning"].get("language") is not None:
            translated_value.update(
                {
                    entry["field"]: entry["value"]
                    for entry in translations or []
                    if entry["language"] == doc["planning"]["language"]
                }
            )

            translated_name = translated_value.get("name", translated_value.get("headline"))
            doc["planning"].update(
                {
                    key: val
                    for key, val in translated_value.items()
                    if key in ("ednote", "description_text", "headline", "slugline", "authors", "internal_note")
                    and doc["planning"].get(key) is None
                }
            )

        if not assigned_to.get("assignment_id") and (assigned_to.get("user") or assigned_to.get("desk")):
            # Creating a new assignment
            assign_state = ASSIGNMENT_WORKFLOW_STATE.DRAFT if is_coverage_draft else ASSIGNMENT_WORKFLOW_STATE.ASSIGNED
            if not is_coverage_draft:
                # In case of article_rewrites, this will be 'in_progress' directly
                if assigned_to.get("state") and assigned_to["state"] != ASSIGNMENT_WORKFLOW_STATE.DRAFT:
                    assign_state = assigned_to.get("state")

            if translated_value and translated_name and "headline" not in doc["planning"]:
                doc["planning"]["headline"] = translated_name

            assignment = {
                "assigned_to": {
                    "user": assigned_to.get("user"),
                    "desk": assigned_to.get("desk"),
                    "contact": assigned_to.get("contact"),
                    "state": assign_state,
                },
                "planning_item": planning_id,
                "coverage_item": doc.get("coverage_id"),
                "planning": deepcopy(doc.get("planning")),
                "priority": assigned_to.get("priority", DEFAULT_ASSIGNMENT_PRIORITY),
                "description_text": planning.get("description_text"),
            }
            if translated_value and translated_name and assignment.get("name") != translated_value.get("name"):
                assignment["name"] = translated_name

            if doc.get("scheduled_update_id"):
                assignment["scheduled_update_id"] = doc["scheduled_update_id"]
                assignment["planning"] = deepcopy(parent_coverage.get("planning"))
                assignment["planning"].update(doc.get("planning"))

            if "coverage_provider" in assigned_to:
                assignment["assigned_to"]["coverage_provider"] = assigned_to.get("coverage_provider")

            if TO_BE_CONFIRMED_FIELD in doc:
                assignment["planning"][TO_BE_CONFIRMED_FIELD] = doc[TO_BE_CONFIRMED_FIELD]

            new_assignment_id = str(assignment_service.post([assignment])[0])
            updates["assigned_to"]["assignment_id"] = new_assignment_id
            updates["assigned_to"]["state"] = assign_state
        elif assigned_to.get("assignment_id"):
            self.set_xmp_file_info(updates, original)

            if not updates.get("assigned_to"):
                if planning_original.get("state") == WORKFLOW_STATE.CANCELLED or coverage_status not in [
                    WORKFLOW_STATE.CANCELLED,
                    WORKFLOW_STATE.DRAFT,
                ]:
                    raise SuperdeskApiError.badRequestError("Coverage not in correct state to remove assignment.")
                # Removing assignment
                assignment_service.delete(lookup={"_id": assigned_to.get("assignment_id")})
                assignment = {
                    "planning_item": planning_original.get(config.ID_FIELD),
                    "coverage_item": doc.get("coverage_id"),
                }
                if doc.get("scheduled_update"):
                    assignment["scheduled_update_id"] = doc.get("scheduled_update_id")

                get_resource_service("assignments_history").on_item_deleted(assignment)
                return

            # update the assignment using the coverage details
            original_assignment = assignment_service.find_one(req=None, _id=assigned_to.get("assignment_id"))

            if not original_assignment:
                raise SuperdeskApiError.badRequestError("Assignment related to the coverage does not exists.")

            # Check if coverage was cancelled
            coverage_cancel_state = get_coverage_status_from_cv("ncostat:notint")
            coverage_cancel_state.pop("is_active", None)
            if (
                original.get("workflow_status") != updates.get("workflow_status")
                and updates.get("workflow_status") == WORKFLOW_STATE.CANCELLED
            ):
                self.cancel_coverage(
                    updates,
                    coverage_cancel_state,
                    original.get("workflow_status"),
                    original_assignment,
                    updates.get("planning").get("workflow_status_reason"),
                )
                return

            assignment = {}
            if self.is_coverage_planning_modified(updates, original):
                assignment["planning"] = deepcopy(doc.get("planning"))

                if TO_BE_CONFIRMED_FIELD in doc:
                    assignment["planning"][TO_BE_CONFIRMED_FIELD] = doc[TO_BE_CONFIRMED_FIELD]

            if original_assignment.get("assigned_to").get("state") == ASSIGNMENT_WORKFLOW_STATE.DRAFT:
                if self.is_coverage_assignment_modified(updates, original_assignment):
                    user = get_user()
                    assignment["priority"] = assigned_to.pop("priority", original_assignment.get("priority"))
                    assignment["assigned_to"] = assigned_to
                    if original_assignment.get("assigned_to", {}).get("desk") != assigned_to.get("desk"):
                        assigned_to["assigned_date_desk"] = utcnow()
                        assigned_to["assignor_desk"] = user.get(config.ID_FIELD)
                    if assigned_to.get("user") and original.get("assigned_to", {}).get("user") != assigned_to.get(
                        "user"
                    ):
                        assigned_to["assigned_date_user"] = utcnow()
                        assigned_to["assignor_user"] = user.get(config.ID_FIELD)

            # If we made a coverage 'active' - change assignment status to active
            if original.get("workflow_status") == WORKFLOW_STATE.DRAFT and not is_coverage_draft:
                assigned_to["state"] = ASSIGNMENT_WORKFLOW_STATE.ASSIGNED
                assignment["assigned_to"] = assigned_to

            # If the Planning description has been changed
            if planning_original.get("description_text") != planning_updates.get("description_text"):
                assignment["description_text"] = planning["description_text"]

            # If the Planning name has been changed
            if planning_original.get("name") != planning_updates.get("name"):
                assignment["name"] = planning["name"] if not translated_value and translated_name else translated_name

            # If the coverage assignee has been changed and workflow status is active
            if original.get("workflow_status") != WORKFLOW_STATE.DRAFT and self.is_coverage_assignment_modified(
                updates, original_assignment
            ):
                assigned_to["state"] = ASSIGNMENT_WORKFLOW_STATE.ASSIGNED
                assignment["assigned_to"] = assigned_to

            # If there has been a change in the planning internal note then notify the assigned users/desk
            if planning_updates.get("internal_note") and planning_original.get("internal_note") != planning_updates.get(
                "internal_note"
            ):
                PlanningNotifications().notify_assignment(
                    coverage_status=updates.get("workflow_status"),
                    target_desk=assigned_to.get("desk") if assigned_to.get("user") is None else None,
                    target_user=assigned_to.get("user"),
                    contact_id=assigned_to.get("contact"),
                    message="assignment_planning_internal_note_msg",
                    coverage_type=get_coverage_type_name(updates.get("planning", {}).get("g2_content_type", "")),
                    slugline=planning.get("slugline", ""),
                    internal_note=planning.get("internal_note", ""),
                    no_email=True,
                )

            # Update only if anything got modified
            if (
                "planning" in assignment
                or "assigned_to" in assignment
                or "description_text" in assignment
                or "name" in assignment
            ):
                assignment_service.system_update(
                    ObjectId(assigned_to.get("assignment_id")),
                    assignment,
                    original_assignment,
                )

            if self.is_xmp_updated(updates, original):
                PlanningNotifications().notify_assignment(
                    coverage_status=updates.get("workflow_status"),
                    target_desk=assigned_to.get("desk") if assigned_to.get("user") is None else None,
                    target_user=assigned_to.get("user"),
                    contact_id=assigned_to.get("contact"),
                    message="assignment_planning_xmp_file_msg",
                    meta_message="assignment_details_email",
                    coverage_type=get_coverage_type_name(updates.get("planning", {}).get("g2_content_type", "")),
                    slugline=planning.get("slugline", ""),
                    assignment=assignment,
                )

    def cancel_coverage(
        self,
        coverage,
        coverage_cancel_state,
        original_workflow_status,
        assignment=None,
        reason=None,
        event_cancellation=False,
        event_reschedule=False,
    ):
        self._perform_coverage_cancel(
            coverage,
            coverage_cancel_state,
            original_workflow_status,
            assignment,
            reason,
            event_cancellation,
            event_reschedule,
        )

        for s in coverage.get("scheduled_updates") or []:
            self._perform_coverage_cancel(
                s,
                coverage_cancel_state,
                original_workflow_status,
                None,
                reason,
                event_cancellation,
                event_reschedule,
            )

    def _perform_coverage_cancel(
        self,
        coverage,
        coverage_cancel_state,
        original_workflow_status,
        assignment,
        reason,
        event_cancellation,
        event_reschedule,
    ):
        # If coverage is already cancelled, don't change it's state_reason
        if coverage.get("previous_status"):
            return

        coverage["news_coverage_status"] = coverage_cancel_state
        coverage["previous_status"] = original_workflow_status
        coverage["workflow_status"] = WORKFLOW_STATE.CANCELLED
        coverage["planning"]["workflow_status_reason"] = reason

        # Cancel assignment if the coverage has an assignment
        if coverage.get("assigned_to"):
            coverage["assigned_to"]["state"] = WORKFLOW_STATE.CANCELLED
            assignment_service = get_resource_service("assignments")
            if not assignment:
                assignment = assignment_service.find_one(req=None, _id=coverage["assigned_to"].get("assignment_id"))

            if assignment:
                assignment_service.cancel_assignment(assignment, coverage, event_cancellation, event_reschedule)

    def duplicate_coverage_for_article_rewrite(self, planning_id, coverage_id, updates):
        planning = self.find_one(req=None, _id=planning_id)

        if not planning:
            raise SuperdeskApiError.badRequestError("Planning does not exist")

        self.generate_related_assignments([planning])
        coverages = planning.get("coverages") or []
        try:
            coverage = next(c for c in coverages if c.get("coverage_id") == coverage_id)
        except StopIteration:
            raise SuperdeskApiError.badRequestError("Coverage does not exist")

        coverage_planning = coverage.get("planning") or {}
        updates_planning = updates.get("planning") or {}
        coverages.append(
            {
                "planning": {
                    "g2_content_type": updates_planning.get("g2_content_type")
                    or coverage_planning.get("g2_content_type"),
                    "slugline": updates_planning.get("slugline") or coverage_planning.get("slugline"),
                    "scheduled": updates_planning.get("scheduled") or coverage_planning.get("scheduled"),
                },
                "news_coverage_status": updates.get("news_coverage_status") or coverage.get("news_coverage_status"),
                "workflow_status": WORKFLOW_STATE.ACTIVE,
                "assigned_to": updates.get("assigned_to") or coverage.get("assigned_to"),
            }
        )

        coverage_ids = [c["coverage_id"] for c in coverages if c.get("coverage_id")]
        new_plan = self.patch(planning[config.ID_FIELD], {"coverages": coverages})

        try:
            new_coverage = next(c for c in new_plan["coverages"] if c.get("coverage_id") not in coverage_ids)
        except StopIteration:
            raise SuperdeskApiError.badRequestError("New coverage was not found!")

        planning.update(new_plan)
        return planning, new_coverage

    def remove_assignment(self, assignment_item):
        coverage_id = assignment_item.get("coverage_item")
        planning_item = self.find_one(req=None, _id=assignment_item.get("planning_item"))

        if not planning_item or assignment_item.get("_to_delete"):
            return planning_item

        coverages = planning_item.get("coverages") or []
        try:
            coverage_item = next(c for c in coverages if c.get("coverage_id") == coverage_id)
        except StopIteration:
            raise SuperdeskApiError.badRequestError("Coverage does not exist")

        if not coverage_item.get("assigned_to"):
            # Assignment was already removed (unposting a planning item scenario)
            return planning_item

        for s in coverage_item.get("scheduled_updates"):
            assigned_to = s.get("assigned_to")
            PlanningNotifications().notify_assignment(
                coverage_status=s.get("workflow_status"),
                target_desk=assigned_to.get("desk") if assigned_to.get("user") is None else None,
                target_user=assigned_to.get("user"),
                message="assignment_removed_msg",
                coverage_type=get_coverage_type_name(coverage_item.get("planning", {}).get("g2_content_type", "")),
                slugline=planning_item.get("slugline", ""),
            )
            del s["assigned_to"]
            s["workflow_status"] = WORKFLOW_STATE.DRAFT

        assigned_to = assignment_item.get("assigned_to")
        PlanningNotifications().notify_assignment(
            coverage_status=coverage_item.get("workflow_status"),
            target_desk=assigned_to.get("desk") if assigned_to.get("user") is None else None,
            target_user=assigned_to.get("user"),
            message="assignment_removed_msg",
            coverage_type=get_coverage_type_name(coverage_item.get("planning", {}).get("g2_content_type", "")),
            slugline=planning_item.get("slugline", ""),
        )
        del coverage_item["assigned_to"]
        coverage_item["workflow_status"] = WORKFLOW_STATE.DRAFT

        updated_planning = self.system_update(planning_item[config.ID_FIELD], {"coverages": coverages}, planning_item)

        get_resource_service("planning_autosave").on_assignment_removed(planning_item[config.ID_FIELD], coverage_id)

        if planning_item.get("event_item"):
            updated_planning["event_item"] = planning_item["event_item"]

        return updated_planning

    def is_coverage_planning_modified(self, updates, original):
        for key in updates.get("planning").keys():
            if not key.startswith("_") and updates.get("planning")[key] != (original.get("planning") or {}).get(key):
                return True

        if (
            TO_BE_CONFIRMED_FIELD in original
            and TO_BE_CONFIRMED_FIELD in updates
            and original[TO_BE_CONFIRMED_FIELD] != updates[TO_BE_CONFIRMED_FIELD]
        ):
            return True

        return False

    def is_coverage_assignment_modified(self, updates, original):
        if (updates or {}).get("assigned_to"):
            keys = ["desk", "user", "state", "coverage_provider"]
            for key in keys:
                if key in updates.get("assigned_to") and updates["assigned_to"][key] != (
                    original.get("assigned_to") or {}
                ).get(key):
                    return True

            if updates["assigned_to"].get("priority") and updates["assigned_to"]["priority"] != original.get(
                "priority"
            ):
                return True

        return False

    def delete_assignments_for_coverages(self, coverages, notify=True):
        failed_assignments = []
        deleted_assignments = []
        assignment_service = get_resource_service("assignments")
        for coverage in coverages:
            assign_id = coverage["assigned_to"].get("assignment_id")
            if not assign_id:
                continue
            assign_planning = coverage.get("planning")
            try:
                assignment_service.delete_action(lookup={"_id": assign_id})
                deleted_assignments.append(
                    {
                        "id": assign_id,
                        "slugline": assign_planning.get("slugline"),
                        "type": assign_planning.get("g2_content_type"),
                    }
                )
            except AssignmentApiError as e:
                logger.error("There is a assignment '{}' is in progress".format(assign_id))
                failed_assignments.append(
                    {
                        "state": "in Progress",
                        "slugline": assign_planning.get("slugline"),
                        "type": assign_planning.get("g2_content_type"),
                    }
                )
            except SuperdeskApiError as e:
                failed_assignments.append(
                    {
                        "error": str(e),
                        "slugline": assign_planning.get("slugline"),
                        "type": assign_planning.get("g2_content_type"),
                    }
                )
                # Mark the assignment to be deleted.
                original_assigment = assignment_service.find_one(req=None, _id=assign_id)
                if original_assigment:
                    assignment_service.system_update(ObjectId(assign_id), {"_to_delete": True}, original_assigment)

        if request:
            session_id = get_auth().get("_id")
            user_id = get_user().get(config.ID_FIELD)
            if len(deleted_assignments) > 0:
                push_notification(
                    "assignments:delete",
                    items=deleted_assignments,
                    session=session_id,
                    user=user_id,
                )

            if len(failed_assignments) > 0 and notify:
                push_notification(
                    "assignments:delete:fail",
                    items=failed_assignments,
                    session=session_id,
                    user=user_id,
                )

    def get_expired_items(self, expiry_datetime, spiked_planning_only=False):
        """Get the expired items

        Where planning_date is in the past
        """
        nested_filter = {
            "nested": {
                "path": "_planning_schedule",
                "filter": {"range": {"_planning_schedule.scheduled": {"gt": date_to_str(expiry_datetime)}}},
            }
        }
        range_filter = {"range": {"planning_date": {"gt": date_to_str(expiry_datetime)}}}
        query = {
            "query": {
                "bool": {
                    "must_not": [
                        {"constant_score": {"filter": {"exists": {"field": "event_item"}}}},
                        {"term": {"expired": True}},
                        nested_filter,
                        range_filter,
                    ]
                }
            }
        }

        if spiked_planning_only:
            query = {
                "query": {
                    "bool": {
                        "must_not": [nested_filter, range_filter],
                        "must": [{"term": {"state": WORKFLOW_STATE.SPIKED}}],
                    }
                }
            }

        query["sort"] = [{"planning_date": "asc"}]
        query["size"] = 200

        total_received = 0
        total_items = -1

        while True:
            query["from"] = total_received

            results = self.search(query)

            # If the total_items has not been set, then this is the first query
            # In which case we need to store the total hits from the search
            if total_items < 0:
                total_items = results.count()

                # If the search doesn't contain any results, return here
                if total_items < 1:
                    break

            # If the last query doesn't contain any results, return here
            if not len(results.docs):
                break

            total_received += len(results.docs)

            # Yield the results for iteration by the callee
            yield list(results.docs)

    def on_event_converted_to_recurring(self, updates, original):
        items = self.find(where={"event_item": original[config.ID_FIELD]})

        for item in items:
            self.patch(item[config.ID_FIELD], {"recurrence_id": updates["recurrence_id"]})

    def get_xmp_file_for_updates(self, updates_coverage, original_coverage, for_slugline=False):
        rv = False
        if not (updates_coverage["planning"] or {}).get("xmp_file"):
            return rv

        if not get_coverage_type_name((updates_coverage.get("planning") or {}).get("g2_content_type")) in [
            "Picture",
            "picture",
        ]:
            return rv

        if not self.is_xmp_updated(updates_coverage, original_coverage):
            return rv

        coverage_id = updates_coverage.get("coverage_id") or (original_coverage or {}).get("coverage_id")
        xmp_file = get_resource_service("planning_files").find_one(
            req=None, _id=updates_coverage["planning"]["xmp_file"]
        )
        if not xmp_file:
            logger.error(
                "Attached xmp_file not found. Coverage: {0}, xmp_file: {1}".format(
                    coverage_id, updates_coverage["planning"]["xmp_file"]
                )
            )
            return rv

        xmp_file = app.media.get(xmp_file["media"], resource="planning_files")
        if not xmp_file:
            logger.error(
                "xmp_file not found in media storage. Coverage: {0}, xmp_file: {1}".format(
                    coverage_id, updates_coverage["planning"]["xmp_file"]
                )
            )
            return rv

        if for_slugline:
            if not get_planning_use_xmp_for_pic_slugline(app) or not get_planning_xmp_slugline_mapping(app):
                return rv
        else:
            if (
                not (updates_coverage.get("assigned_to") or {}).get("assignment_id")
                and not updates_coverage.get("type") == "assignment"
            ):
                return rv

            if not get_planning_use_xmp_for_pic_assignments(app) or not get_planning_xmp_assignment_mapping(app):
                return rv

        return xmp_file

    def set_slugline_from_xmp(self, updates_coverage, original_coverage=None):
        xmp_file = self.get_xmp_file_for_updates(updates_coverage, original_coverage, for_slugline=True)
        if not xmp_file:
            return

        parsed = etree.parse(xmp_file)
        xmp_slugline_mapping = get_planning_xmp_slugline_mapping(app)
        tags = parsed.xpath(xmp_slugline_mapping["xpath"], namespaces=xmp_slugline_mapping["namespaces"])
        if tags:
            updates_coverage["planning"]["slugline"] = tags[0].text

    def is_xmp_updated(self, updates_coverage, original_coverage=None):
        return (
            updates_coverage["planning"].get("xmp_file")
            and ((original_coverage or {}).get("planning") or {}).get("xmp_file")
            != updates_coverage["planning"]["xmp_file"]
        )

    def set_xmp_file_info(self, updates_coverage, original_coverage=None):
        xmp_file = self.get_xmp_file_for_updates(updates_coverage, original_coverage)
        if not xmp_file:
            return

        assignment_id = updates_coverage.get("_id") or updates_coverage["assigned_to"].get("assignment_id")
        try:
            mapped = False
            parsed = etree.parse(xmp_file)
            xmp_assignment_mapping = get_planning_xmp_assignment_mapping(app)
            tags = parsed.xpath(
                xmp_assignment_mapping["xpath"],
                namespaces=xmp_assignment_mapping["namespaces"],
            )
            if tags:
                tags[0].attrib[xmp_assignment_mapping["atribute_key"]] = assignment_id
                mapped = True

            if not mapped:
                parent_xpath = xmp_assignment_mapping["xpath"][0 : xmp_assignment_mapping["xpath"].rfind("/")]
                parent = parsed.xpath(parent_xpath, namespaces=xmp_assignment_mapping["namespaces"])
                if parent:
                    elem = etree.SubElement(
                        parent[0],
                        "{{{0}}}Description".format(xmp_assignment_mapping["namespaces"]["rdf"]),
                        nsmap=xmp_assignment_mapping["namespaces"],
                    )
                    elem.attrib[xmp_assignment_mapping["atribute_key"]] = assignment_id
                else:
                    logger.error("Cannot find xmp_mapping path in XMP file for assignment: {}".format(assignment_id))
                    return

            buf = BytesIO()
            buf.write(etree.tostring(parsed.getroot(), pretty_print=True))
            buf.seek(0)
            media_id = app.media.put(
                buf,
                resource="planning_files",
                filename=xmp_file.filename,
                content_type="application/octet-stream",
            )
            get_resource_service("planning_files").patch(
                updates_coverage["planning"]["xmp_file"],
                {"filemeta": {"media_id": media_id}, "media": media_id},
            )
            push_notification("planning_files:updated", item=updates_coverage["planning"]["xmp_file"])
        except Exception:
            logger.error(
                "Error while injecting assignment ID to XMP File. Assignment: {0}, xmp_file: {1}".format(
                    assignment_id, updates_coverage["planning"]["xmp_file"]
                )
            )

    def duplicate_xmp_file(self, coverage):
        cov_plan = coverage.get("planning") or {}
        if not (
            cov_plan.get("xmp_file")
            and get_coverage_type_name(cov_plan.get("g2_content_type")) in ["Picture", "picture"]
        ):
            return

        file_id = coverage["planning"]["xmp_file"]
        xmp_file = get_resource_service("planning_files").find_one(req=None, _id=file_id)
        coverage_msg = "Duplicating Coverage: {}".format(coverage["coverage_id"])
        if not xmp_file:
            logger.error("XMP File {} attached to coverage not found. {}".format(file_id, coverage_msg))
            return

        xmp_file = app.media.get(xmp_file["media"], resource="planning_files")
        if not xmp_file:
            logger.error("Media file for XMP File {} not found. {}".format(file_id, coverage_msg))
            return

        try:
            buf = BytesIO()
            buf.write(xmp_file.read())
            buf.seek(0)
            media_id = app.media.put(
                buf,
                resource="planning_files",
                filename=xmp_file.name,
                content_type="application/octet-stream",
            )
        except Exception as e:
            logger.exception("Error creating media file. {}. Exception: {}".format(coverage_msg, e))
        planning_file_ids = get_resource_service("planning_files").post([{"media": media_id}])
        coverage["planning"]["xmp_file"] = planning_file_ids[0]

    def _update_recurring_planning_items(self, updates, original, update_method):
        SKIP_PLANNING_FIELDS = {
            "_id",
            "guid",
            "unique_id",
            "original_creator",
            "firstcreated",
            "lock_user",
            "lock_time",
            "lock_session",
            "lock_action",
            "revert_state",
            "ingest_provider",
            "source",
            "original_source",
            "ingest_provider_sequence",
            "ingest_firstcreated",
            "ingest_versioncreated",
            "event_item",
            "state",
            "pubstatus",
            "expiry",
            "expired",
            "featured",
            "_planning_schedule",
            "_updates_schedule",
            "planning_date",
            "state_reason",
        }
        SKIP_COVERAGE_FIELDS = {
            "coverage_id",
            "original_coverage_id",
            "guid",
            "original_creator",
            "firstcreated",
            "previous_status",
        }
        for plan in self._iter_recurring_plannings_to_update(updates, original, update_method):
            plan_updates = deepcopy(updates)
            for field in SKIP_PLANNING_FIELDS:
                plan_updates.pop(field, None)

            try:
                planning_date_diff = updates["planning_date"] - original["planning_date"]
                if planning_date_diff:
                    plan_updates["planning_date"] = plan["planning_date"] + planning_date_diff
            except KeyError:
                pass

            if len(updates.get("coverages") or []) and len(plan.get("coverages") or []):
                plan_updates["coverages"] = deepcopy(plan["coverages"])
                for coverage in plan_updates["coverages"]:
                    try:
                        original_coverage_id = coverage["original_coverage_id"]
                    except KeyError:
                        continue

                    coverage_updates = get_coverage_by_id(updates, original_coverage_id, "original_coverage_id")
                    if coverage_updates is None:
                        continue

                    for field, value in coverage_updates.items():
                        if field in SKIP_COVERAGE_FIELDS:
                            continue
                        elif field == "assigned_to":
                            if coverage.get("workflow_status") != WORKFLOW_STATE.DRAFT:
                                # This coverage has already been added to the workflow
                                # ``assigned_to`` information should be managed from the Assignment not Coverage
                                continue

                            # Copy the ``assigned_to`` data, keeping the original ``assignment_id`` (if any)
                            original_assignment_id = coverage.get("assignment_id")
                            coverage[field] = deepcopy(value)
                            if original_assignment_id is not None:
                                coverage[field]["assignment_id"] = original_assignment_id
                        elif field == "planning":
                            original_scheduled = (coverage.get("planning") or {}).get("scheduled")
                            coverage["planning"] = deepcopy(value)
                            coverage_original = get_coverage_by_id(
                                original, original_coverage_id, "original_coverage_id"
                            )
                            if coverage_original is not None:
                                scheduled_diff = value["scheduled"] - coverage_original["planning"]["scheduled"]
                                coverage["planning"]["scheduled"] = original_scheduled + scheduled_diff
                            else:
                                coverage["planning"]["scheduled"] = original_scheduled
                        else:
                            coverage[field] = deepcopy(value)

                # Add new Coverages that were added during this update request
                for coverage in updates["coverages"]:
                    if get_coverage_by_id(original, coverage["coverage_id"]) is not None:
                        # Skip this one, as this Coverage exists in the original
                        continue

                    new_coverage = deepcopy(coverage)
                    for field in SKIP_COVERAGE_FIELDS:
                        new_coverage.pop(field, None)

                    # Remove the Assignment ID (if any)
                    try:
                        new_coverage["assigned_to"].pop("assignment_id", None)
                    except (KeyError, TypeError):
                        pass

                    # Set the new scheduled date, relative to the planning date
                    try:
                        plan_date = plan_updates.get("planning_date") or plan["planning_date"]
                        if plan_date:
                            scheduled_diff = coverage["planning"]["scheduled"] - (
                                updates.get("planning_date") or original.get("planning_date")
                            )
                            new_coverage["planning"]["scheduled"] = plan_date + scheduled_diff
                    except (KeyError, TypeError):
                        pass

                    plan_updates["coverages"].append(new_coverage)

            self.patch(plan["_id"], plan_updates)
            app.on_updated_planning(plan_updates, {"_id": plan["_id"]})

    def _iter_recurring_plannings_to_update(self, updates, original, update_method):
        selected_start = updates.get("planning_date") or original.get("planning_date")
        # Make sure we are working with a datetime instance
        if not isinstance(selected_start, datetime):
            selected_start = datetime.strptime(selected_start, "%Y-%m-%dT%H:%M:%S%z")

        try:
            lookup = {"planning_recurrence_id": original["planning_recurrence_id"]}
        except KeyError:
            return

        for plan in self.get_from_mongo(req=None, lookup=lookup):
            if plan["_id"] == original["_id"]:
                # Skip this Planning item, as it is the same item provided to the update request
                continue
            elif update_method == UPDATE_FUTURE and plan["planning_date"] < selected_start:
                continue
            yield plan


event_type = deepcopy(superdesk.Resource.rel("events", type="string"))
event_type["mapping"] = not_analyzed

assigned_to_schema = {
    "type": "dict",
    "mapping": {
        "type": "object",
        "properties": {
            "assignment_id": not_analyzed,
            "state": not_analyzed,
            "contact": not_analyzed,
        },
    },
}

coverage_schema = {
    # Identifiers
    "coverage_id": {"type": "string", "mapping": not_analyzed},
    "original_coverage_id": {"type": "string", "mapping": not_analyzed},
    "guid": metadata_schema["guid"],
    # Audit Information
    "original_creator": metadata_schema["original_creator"],
    "version_creator": metadata_schema["version_creator"],
    "firstcreated": metadata_schema["firstcreated"],
    "versioncreated": metadata_schema["versioncreated"],
    # News Coverage Details
    # See IPTC-G2-Implementation_Guide 16.4
    "planning": {
        "type": "dict",
        "schema": {
            "ednote": metadata_schema["ednote"],
            "g2_content_type": {"type": "string", "mapping": not_analyzed},
            "coverage_provider": {"type": "string", "mapping": not_analyzed},
            "contact_info": Resource.rel("contacts", type="string", nullable=True),
            "item_class": {"type": "string", "mapping": not_analyzed},
            "item_count": {"type": "string", "mapping": not_analyzed},
            "scheduled": {"type": "datetime"},
            "files": {
                "type": "list",
                "nullable": True,
                "schema": Resource.rel("planning_files"),
                "mapping": not_analyzed,
            },
            "xmp_file": Resource.rel("planning_files", nullable=True),
            "service": {
                "type": "list",
                "mapping": {"properties": {"qcode": not_analyzed, "name": not_analyzed}},
            },
            "news_content_characteristics": {
                "type": "list",
                "mapping": {"properties": {"name": not_analyzed, "value": not_analyzed}},
            },
            "planning_ext_property": {
                "type": "list",
                "mapping": {
                    "properties": {
                        "qcode": not_analyzed,
                        "value": not_analyzed,
                        "name": not_analyzed,
                    }
                },
            },
            # Metadata hints.  See IPTC-G2-Implementation_Guide 16.5.1.1
            "by": {"type": "list", "mapping": {"type": "string"}},
            "credit_line": {"type": "list", "mapping": {"type": "string"}},
            "dateline": {"type": "list", "mapping": {"type": "string"}},
            "description_text": metadata_schema["description_text"],
            "genre": metadata_schema["genre"],
            "headline": metadata_schema["headline"],
            "keyword": {"type": "list", "mapping": {"type": "string"}},
            "language": metadata_schema["language"],
            "slugline": metadata_schema["slugline"],
            "subject": metadata_schema["subject"],
            "internal_note": {"type": "string", "nullable": True},
            "workflow_status_reason": {"type": "string", "nullable": True},
            "priority": metadata_schema["priority"],
        },  # end planning dict schema
    },  # end planning
    "news_coverage_status": {
        "type": "dict",
        "allow_unknown": True,
        "schema": {
            "qcode": {"type": "string"},
            "name": {"type": "string"},
            "label": {"type": "string"},
        },
    },
    "workflow_status": {"type": "string"},
    "previous_status": {"type": "string"},
    "assigned_to": assigned_to_schema,
    "flags": {
        "type": "dict",
        "allow_unknown": True,
        "schema": {"no_content_linking": {"type": "boolean", "default": False}},
    },
    TO_BE_CONFIRMED_FIELD: TO_BE_CONFIRMED_FIELD_SCHEMA,
    "scheduled_updates": {
        "type": "list",
        "schema": {
            "type": "dict",
            "schema": {
                "scheduled_update_id": {"type": "string", "mapping": not_analyzed},
                "coverage_id": {"type": "string", "mapping": not_analyzed},
                "workflow_status": {"type": "string"},
                "assigned_to": assigned_to_schema,
                "previous_status": {"type": "string"},
                "news_coverage_status": {
                    "type": "dict",
                    "allow_unknown": True,
                    "schema": {
                        "qcode": {"type": "string"},
                        "name": {"type": "string"},
                        "label": {"type": "string"},
                    },
                },
                "planning": {
                    "type": "dict",
                    "schema": {
                        "internal_note": {"type": "string", "nullable": True},
                        "contact_info": Resource.rel("contacts", type="string", nullable=True),
                        "scheduled": {"type": "datetime"},
                        "genre": metadata_schema["genre"],
                        "workflow_status_reason": {"type": "string", "nullable": True},
                    },
                },
            },
        },
    },  # end scheduled_updates
}  # end coverage_schema

planning_schema = {
    # Identifiers
    config.ID_FIELD: metadata_schema[config.ID_FIELD],
    "guid": metadata_schema["guid"],
    # Audit Information
    "original_creator": metadata_schema["original_creator"],
    "version_creator": metadata_schema["version_creator"],
    "firstcreated": metadata_schema["firstcreated"],
    "versioncreated": metadata_schema["versioncreated"],
    # Ingest Details
    "ingest_provider": metadata_schema["ingest_provider"],
    "source": metadata_schema["source"],
    "original_source": metadata_schema["original_source"],
    "ingest_provider_sequence": metadata_schema["ingest_provider_sequence"],
    "ingest_firstcreated": metadata_schema["versioncreated"],
    "ingest_versioncreated": metadata_schema["versioncreated"],
    "ingest_pubstatus": {"type": "string", "mapping": not_analyzed},
    # Agenda Item details
    "agendas": {
        "type": "list",
        "schema": superdesk.Resource.rel("agenda"),
        "mapping": not_analyzed,
    },
    # Event Item
    "event_item": event_type,
    "recurrence_id": {
        "type": "string",
        "mapping": not_analyzed,
        "nullable": True,
    },
    "planning_recurrence_id": {
        "type": "string",
        "mapping": not_analyzed,
        "nullable": True,
    },
    # Planning Details
    # NewsML-G2 Event properties See IPTC-G2-Implementation_Guide 16
    # Planning Item Metadata - See IPTC-G2-Implementation_Guide 16.1
    "item_class": {"type": "string", "default": "plinat:newscoverage"},
    "ednote": metadata_schema["ednote"],
    "description_text": metadata_schema["description_text"],
    "internal_note": {"type": "string", "nullable": True},
    "anpa_category": metadata_schema["anpa_category"],
    "subject": {
        "type": "list",
        "mapping": {
            "type": "object",
            "dynamic": False,
            "properties": {
                "qcode": not_analyzed,
                "name": {
                    "type": "keyword",
                    "fields": {
                        "analyzed": string_with_analyzer,
                    },
                },
                "scheme": not_analyzed,
                "translations": {
                    "type": "object",
                    "dynamic": False,
                    "properties": {"name": {"type": "object", "dynamic": True}},
                },
            },
        },
    },
    "genre": metadata_schema["genre"],
    "company_codes": metadata_schema["company_codes"],
    # Content Metadata - See IPTC-G2-Implementation_Guide 16.2
    "language": metadata_schema["language"],
    "languages": {
        "type": "list",
        "mapping": not_analyzed,
    },
    "translations": {
        "type": "list",
        "mapping": {
            "type": "nested",
            "properties": {
                "field": not_analyzed,
                "language": not_analyzed,
                "value": metadata_schema["slugline"]["mapping"],
            },
        },
    },
    "abstract": metadata_schema["abstract"],
    "headline": metadata_schema["headline"],
    "slugline": metadata_schema["slugline"],
    "keywords": metadata_schema["keywords"],
    "word_count": metadata_schema["word_count"],
    "priority": metadata_schema["priority"],
    "urgency": metadata_schema["urgency"],
    "profile": metadata_schema["profile"],
    # These next two are for spiking/unspiking and purging of planning/agenda items
    "state": WORKFLOW_STATE_SCHEMA,
    "expiry": {"type": "datetime", "nullable": True},
    "expired": {"type": "boolean", "default": False},
    "featured": {"type": "boolean"},
    "lock_user": metadata_schema["lock_user"],
    "lock_time": metadata_schema["lock_time"],
    "lock_session": metadata_schema["lock_session"],
    "lock_action": metadata_schema["lock_action"],
    "coverages": {
        "type": "list",
        "default": [],
        "schema": {
            "type": "dict",
            "schema": coverage_schema,
        },
        "mapping": {
            "type": "nested",
            "properties": {
                "coverage_id": not_analyzed,
                "planning": {
                    "type": "object",
                    "properties": {
                        "slugline": metadata_schema["slugline"]["mapping"],
                    },
                },
                "assigned_to": assigned_to_schema["mapping"],
                "original_creator": {
                    "type": "keyword",
                },
            },
        },
    },
    # field to sync coverage scheduled information
    # to be used for sorting/filtering on scheduled
    "_planning_schedule": {
        "type": "list",
        "mapping": {
            "type": "nested",
            "properties": {
                "coverage_id": not_analyzed,
                "scheduled": {"type": "date"},
            },
        },
    },
    # field to sync scheduled_updates scheduled information
    # to be used for sorting/filtering on scheduled
    "_updates_schedule": {
        "type": "list",
        "mapping": {
            "type": "nested",
            "properties": {
                "scheduled_update_id": not_analyzed,
                "scheduled": {"type": "date"},
            },
        },
    },
    "planning_date": {
        "type": "datetime",
        "nullable": False,
    },
    "flags": {
        "type": "dict",
        "schema": {
            "marked_for_not_publication": metadata_schema["flags"]["schema"]["marked_for_not_publication"],
            # If the config is set to create coverage items in workflow this flag will override that and allow coverages
            # created for this planning item to be created in draft
            "overide_auto_assign_to_workflow": {"type": "boolean", "default": False},
        },
    },
    # Public/Published status
    "pubstatus": POST_STATE_SCHEMA,
    # The previous state the item was in before for example being spiked,
    # when un-spiked it will revert to this state
    "revert_state": metadata_schema["revert_state"],
    # Item type used by superdesk publishing
    ITEM_TYPE: {
        "type": "string",
        "mapping": not_analyzed,
        "default": "planning",
    },
    # Identifier used to synchronise the posted planning item with an external system.
    "unique_id": {"type": "string", "mapping": not_analyzed},
    "place": metadata_schema["place"],
    # Name used to identify the planning item
    "name": {"type": "string"},
    "files": {
        "type": "list",
        "nullable": True,
        "schema": Resource.rel("planning_files"),
        "mapping": not_analyzed,
    },
    # Reason (if any) for the current state (cancelled, postponed, rescheduled)
    "state_reason": {"type": "string", "nullable": True},
    TO_BE_CONFIRMED_FIELD: TO_BE_CONFIRMED_FIELD_SCHEMA,
    "_type": {"type": "string", "mapping": None},
    "extra": metadata_schema["extra"],
    "versionposted": {"type": "datetime", "nullable": False},
    # The update method used for recurring planning items
    "update_method": {
        "type": "string",
        "allowed": UPDATE_METHODS,
        "mapping": not_analyzed,
        "nullable": True,
    },
}  # end planning_schema


class PlanningResource(superdesk.Resource):
    """Resource for planning data model

    See IPTC-G2-Implementation_Guide (version 2.21) Section 16.5 for schema details
    """

    endpoint_name = url = "planning"
    item_url = item_url
    schema = planning_schema
    datasource = {
        "source": "planning",
        "search_backend": "elastic",
    }
    resource_methods = ["GET", "POST"]
    item_methods = ["GET", "PATCH", "PUT", "DELETE"]
    privileges = {
        "POST": "planning_planning_management",
        "PATCH": "planning_planning_management",
        "DELETE": "planning",
    }
    etag_ignore_fields = ["_planning_schedule", "_updates_schedule"]

    mongo_indexes = {
        "event_item": ([("event_item", 1)], {"background": True}),
        "planning_recurrence_id": ([("planning_recurrence_id", 1)], {"background": True}),
    }

    merge_nested_documents = True
