from datetime import datetime
from typing import Any
from copy import deepcopy

from apps.archive.common import update_dates_for
from apps.auth import get_user_id
from planning.core.service import BasePlanningAsyncService
from planning.common import (
    get_version_item_for_post,
    enqueue_planning_item,
    set_original_creator,
)
from planning.planning.planning_service import PlanningAsyncService
from planning.types import PlanningFeaturedResourceModel
from superdesk import get_resource_service, logger
from superdesk.core import get_app_config
from superdesk.core.types import SearchRequest
from superdesk.errors import SuperdeskApiError
from superdesk.utc import utc_to_local, utcnow

ID_DATE_FORMAT = "%Y%m%d"


class PlanningFeaturedAsyncService(BasePlanningAsyncService[PlanningFeaturedResourceModel]):
    """Async Service class for the planning featured model."""

    async def on_create(self, docs: list[PlanningFeaturedResourceModel]):
        for doc in docs:
            doc_dict = doc.to_dict()
            date = utc_to_local(doc_dict.get("tz") or get_app_config("DEFAULT_TIMEZONE"), doc_dict.get("date"))
            _id = date.strftime(ID_DATE_FORMAT)

            search_request = SearchRequest(where={"_id": _id})
            items = await super().find(search_request)
            if await items.count() > 0:
                raise SuperdeskApiError.badRequestError(message="Featured story already exists for this date.")

            await self.validate_featured_attrribute(doc_dict.get("items", []))
            doc_dict["_id"] = _id
            await self.post_featured_planning(doc_dict)
            # set the author
            set_original_creator(doc_dict)

            # set timestamps
            update_dates_for(doc_dict)
            doc = PlanningFeaturedResourceModel(**doc_dict)

    async def on_created(self, docs: list[PlanningFeaturedResourceModel]):
        for doc in docs:
            await self.enqueue_published_item(doc.to_dict(), doc.to_dict())

    async def on_update(self, updates: dict[str, Any], original: PlanningFeaturedResourceModel):
        # Find all planning items in the list
        added_featured = [item_id for item_id in updates.get("items") or [] if item_id not in original.items or []]
        await self.validate_featured_attrribute(added_featured)
        updates["version_creator"] = str(get_user_id())
        await self.post_featured_planning(updates, original.to_dict())

    async def on_updated(self, updates: dict[str, Any], original: PlanningFeaturedResourceModel):
        await self.enqueue_published_item(updates, original.to_dict())

    async def post_featured_planning(self, updates: dict[str, Any], original: dict[str, Any] | None = None):
        if original is None:
            original = {}

        if updates.get("posted", False):
            await self.validate_post_status(updates.get("items", original.get("items", [])))
            updates["posted"] = True
            updates["last_posted_time"] = utcnow()
            updates["last_posted_by"] = str(get_user_id())

    async def enqueue_published_item(self, updates: dict[str, Any], original: dict[str, Any]):
        if updates.get("posted", False):
            plan = deepcopy(original)
            plan.update(updates)
            version, plan = get_version_item_for_post(plan)

            # Create an entry in the planning versions collection for this published version
            version_id = get_resource_service("published_planning").post(
                [
                    {
                        "item_id": plan["_id"],
                        "version": version,
                        "type": "planning_featured",
                        "published_item": plan,
                    }
                ]
            )
            if version_id:
                # Asynchronously enqueue the item for publishing.
                enqueue_planning_item.apply_async(kwargs={"id": version_id[0]}, serializer="eve/json")
            else:
                logger.error("Failed to save planning_featured version for featured item id {}".format(plan["_id"]))

    async def validate_featured_attrribute(self, planning_ids: list):
        planning_service = PlanningAsyncService()
        for planning_id in planning_ids:
            planning_item = await planning_service.find_by_id_raw(planning_id)
            if planning_item and not planning_item.get("featured"):
                raise SuperdeskApiError.badRequestError(message="A planning item in the list is not featured.")

    async def validate_post_status(self, planning_ids: list):
        planning_service = PlanningAsyncService()
        for planning_id in planning_ids:
            planning_item = await planning_service.find_by_id_raw(planning_id)
            if planning_item and not planning_item.get("pubstatus", None):
                raise SuperdeskApiError.badRequestError(
                    message="Not all planning items are posted. Aborting post action."
                )

    def get_id_for_date(self, date: datetime) -> str:
        local_date = utc_to_local(get_app_config("DEFAULT_TIMEZONE"), date)
        return local_date.strftime(ID_DATE_FORMAT)
