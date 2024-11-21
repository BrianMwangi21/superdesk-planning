from superdesk.core.resources import (
    ResourceConfig,
    MongoIndexOptions,
    MongoResourceConfig,
    ElasticResourceConfig,
)

from planning.types import PlanningResourceModel

from .service import PlanningAsyncService

planning_resource_config = ResourceConfig(
    name="planning",
    data_class=PlanningResourceModel,
    service=PlanningAsyncService,
    default_sort=[("dates.start", 1)],
    mongo=MongoResourceConfig(
        indexes=[
            MongoIndexOptions(name="planning_recurrence_id", keys=[("planning_recurrence_id", 1)]),
        ],
    ),
    elastic=ElasticResourceConfig(),
)
