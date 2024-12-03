from typing import Generic, TypeVar

from superdesk.core.resources.service import AsyncResourceService

from planning.types import BasePlanningModel


PlanningResourceModelType = TypeVar("PlanningResourceModelType", bound=BasePlanningModel)


class BasePlanningAsyncService(AsyncResourceService[Generic[PlanningResourceModelType]]):
    pass
