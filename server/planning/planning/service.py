from planning.types import PlanningResourceModel
from planning.core.service import BasePlanningAsyncService


class PlanningAsyncService(BasePlanningAsyncService[PlanningResourceModel]):
    resource_name = "planning"
