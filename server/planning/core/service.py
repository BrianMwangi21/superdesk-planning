from typing import Generic, TypeVar

from superdesk.core.resources.service import AsyncResourceService

from planning.types import PlanningResourceModel


PlanningResourceModelType = TypeVar("PlanningResourceModelType", bound=PlanningResourceModel)


class PlanningAsyncResourceService(AsyncResourceService[Generic[PlanningResourceModelType]]):
    pass
