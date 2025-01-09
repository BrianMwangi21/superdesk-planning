from typing import Generic, TypeVar

from apps.archive.common import get_user
from superdesk.core.resources.service import AsyncResourceService

from planning.types import BasePlanningModel


PlanningResourceModelType = TypeVar("PlanningResourceModelType", bound=BasePlanningModel)


class BasePlanningAsyncService(AsyncResourceService[Generic[PlanningResourceModelType]]):
    async def on_create(self, docs: list[PlanningResourceModelType]) -> None:
        await super().on_create(docs)

        current_user = get_user()
        if current_user:
            for doc in docs:
                doc.original_creator = current_user.id
                doc.version_creator = current_user.id
