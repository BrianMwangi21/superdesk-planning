from superdesk.core.resources import (
    ResourceConfig,
    MongoIndexOptions,
    MongoResourceConfig,
    ElasticResourceConfig,
)

from planning.types import PlanningResourceModel, PlanningHistoryResourceModel

from .service import PlanningAsyncService
from .planning_history_async_service import PlanningHistoryAsyncService

planning_resource_config = ResourceConfig(
    name="planning",
    data_class=PlanningResourceModel,
    service=PlanningAsyncService,
    mongo=MongoResourceConfig(
        indexes=[
            MongoIndexOptions(
                name="planning_recurrence_id",
                keys=[("planning_recurrence_id", 1)],
                unique=False,
            ),
        ],
    ),
    elastic=ElasticResourceConfig(),
)

planning_history_resource_config = ResourceConfig(
    name="planning_history",
    data_class=PlanningHistoryResourceModel,
    service=PlanningHistoryAsyncService,
)
