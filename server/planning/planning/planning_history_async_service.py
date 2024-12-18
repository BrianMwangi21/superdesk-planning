# This file is part of Superdesk.
#
# Copyright 2013, 2014 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Superdesk Files"""

import logging
from copy import deepcopy
from typing import Any


from planning.types import PlanningHistoryResourceModel
from superdesk.flask import request
from superdesk.resource_fields import ID_FIELD
from superdesk.default_settings import strtobool

from planning.types import PlanningResourceModel
from planning.history_async_service import HistoryAsyncService
from planning.common import WORKFLOW_STATE, ITEM_ACTIONS, ASSIGNMENT_WORKFLOW_STATE
from planning.item_lock import LOCK_ACTION
from planning.assignments.assignments_history import ASSIGNMENT_HISTORY_ACTIONS
from planning.utils import (
    get_related_event_links_for_planning,
    is_coverage_planning_modified,
    is_coverage_assignment_modified,
)


logger = logging.getLogger(__name__)
update_item_actions = ["assign_agenda", "add_featured", "remove_featured"]


class PlanningHistoryAsyncService(HistoryAsyncService[PlanningHistoryResourceModel]):
    async def on_item_created(self, items: list[PlanningResourceModel], operation=None):
        add_to_planning = False
        if request and hasattr(request, "args"):
            add_to_planning = strtobool(request.args.get("add_to_planning", "false"))
        await super().on_item_created(items, "add_to_planning" if add_to_planning else None)

    async def _save_history(self, item, update: dict[str, Any], operation: str | None = None):
        user = await self.get_user_id()
        # confirmation could be from external fulfillment, so set the user to the assignor
        if operation == ASSIGNMENT_HISTORY_ACTIONS.CONFIRM and user is None:
            assigned_to = update.get("assigned_to")
            if assigned_to is not None:
                user = update.get(
                    "proxy_user",
                    assigned_to.get("assignor_user", assigned_to.get("assignor_desk")),
                )
        history = {
            "planning_id": item[ID_FIELD],
            "user_id": user,
            "operation": operation,
            "update": update,
        }

        if operation == "create" and update.get("state", "") == "ingested":
            history["operation"] = "ingested"

        await self.create([history])

    async def on_item_updated(
        self, updates: dict[str, Any], original: PlanningResourceModel, operation: str | None = None
    ):
        item = deepcopy(original.to_dict())
        if list(item.keys()) == ["_id"]:
            diff = self._remove_unwanted_fields(updates)
        else:
            diff = await self._changes(original, updates)
            diff.pop("coverages", None)
            if updates:
                item.update(updates)

        if len(diff.keys()) > 0:
            operation = operation or "edited"
            if original.get(LOCK_ACTION) in update_item_actions:
                operation = original.get(LOCK_ACTION)
                if original.get(LOCK_ACTION) == "assign_agenda":
                    diff["agendas"] = [a for a in diff.get("agendas", []) if a not in original.get("agendas", [])]

            if len(get_related_event_links_for_planning(diff, "primary")):
                operation = "create_event"

            await self._save_history(item, diff, operation)

        await self._save_coverage_history(updates, original)

    async def on_cancel(self, updates: dict[str, Any], original):
        await self.on_item_updated(
            updates,
            original,
            "planning_cancel" if original.get("lock_action") in ["planning_cancel", "edit"] else "events_cancel",
        )

    async def _get_coverage_diff(self, updates: dict[str, Any], original):
        diff = {"coverage_id": original.get("coverage_id")}
        cov_plan_diff = await self._changes(original.get("planning"), updates.get("planning"))

        if cov_plan_diff:
            diff["planning"] = cov_plan_diff

        if original.get("news_coverage_status") != updates.get("news_coverage_status"):
            diff["news_coverage_status"] = updates.get("news_coverage_status")

        return diff

    async def _save_coverage_history(self, updates: dict[str, Any], original):
        """Save the coverage history for the planning item"""
        item = deepcopy(original)
        original_coverages = {c.get("coverage_id"): c for c in (original or {}).get("coverages") or []}
        updates_coverages = {c.get("coverage_id"): c for c in (updates or {}).get("coverages") or []}
        added, deleted, updated = [], [], []
        add_to_planning = strtobool(request.args.get("add_to_planning", "false"))

        for coverage_id, coverage in updates_coverages.items():
            original_coverage = original_coverages.get(coverage_id)
            if not original_coverage:
                added.append(coverage)
            elif is_coverage_planning_modified(coverage, original_coverage) or is_coverage_assignment_modified(
                coverage, original_coverage
            ):
                updated.append(coverage)

        deleted = [coverage for cid, coverage in original_coverages.items() if cid not in updates_coverages]

        for cov in added:
            if cov.get("assigned_to", {}).get("state") == ASSIGNMENT_WORKFLOW_STATE.ASSIGNED:
                diff = {"coverage_id": cov.get("coverage_id")}
                diff.update(cov)
                await self._save_history(
                    item,
                    diff,
                    "coverage_created_content" if add_to_planning else "coverage_created",
                )
                await self._save_history(item, diff, "reassigned")
                await self._save_history(item, diff, "add_to_workflow")
            else:
                await self._save_history(item, cov, "coverage_created")

        for cov in updated:
            original_coverage = original_coverages.get(cov.get("coverage_id"))
            diff = await self._get_coverage_diff(cov, original_coverage)
            if len(diff.keys()) > 1:
                await self._save_history(item, diff, "coverage_edited")

            if original_coverage is not None:
                if (
                    cov.get("workflow_status") == WORKFLOW_STATE.CANCELLED
                    and original_coverage.get("workflow_status") != WORKFLOW_STATE.CANCELLED
                ):
                    operation = "coverage_cancelled"
                    diff = {
                        "coverage_id": cov.get("coverage_id"),
                        "workflow_status": cov["workflow_status"],
                    }
                    if not original.get(LOCK_ACTION):
                        operation = "events_cancel"
                    elif (
                        original.get(LOCK_ACTION) == ITEM_ACTIONS.PLANNING_CANCEL
                        or updates.get("state") == WORKFLOW_STATE.CANCELLED
                    ):
                        # If cancelled through item action or through editor
                        operation = "planning_cancel"

                    await self._save_history(item, diff, operation)

                # If assignment was added in an update
                if cov.get("assigned_to", {}).get("assignment_id") and not (
                    original_coverage.get("assigned_to") or {}
                ).get("assignment_id"):
                    diff = {
                        "coverage_id": cov.get("coverage_id"),
                        "assigned_to": cov["assigned_to"],
                    }
                    await self._save_history(item, diff, "coverage_assigned")

        for cov in deleted:
            await self._save_history(item, {"coverage_id": cov.get("coverage_id")}, "coverage_deleted")

    async def on_spike(self, updates: dict[str, Any], original):
        """Spike event

        On spike of a planning item the history of any agendas that the item belongs to will have an entry added to
        their history as effectively the scope of what the agenda contains has changed.
        :param updates:
        :param original:
        :return:
        """
        await super().on_spike(updates, original)

    async def on_unspike(self, updates: dict[str, Any], original):
        await super().on_unspike(updates, original)

    async def on_duplicate(self, parent, duplicate):
        await self._save_history(
            {ID_FIELD: str(parent[ID_FIELD])},
            {"duplicate_id": str(duplicate[ID_FIELD])},
            "duplicate",
        )

    async def on_duplicate_from(self, item: dict[str, Any], duplicate_id):
        new_plan = deepcopy(item)
        new_plan["duplicate_id"] = duplicate_id
        await self._save_history({ID_FIELD: str(item[ID_FIELD])}, new_plan, "duplicate_from")
