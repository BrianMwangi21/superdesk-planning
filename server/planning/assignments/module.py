from superdesk.core.resources import (
    ResourceConfig,
    MongoIndexOptions,
    MongoResourceConfig,
    ElasticResourceConfig,
)

from planning.types import AssignmentResourceModel
from .service import AssingmentsAsyncService

assignments_resource_config = ResourceConfig(
    name="assignments",
    data_class=AssignmentResourceModel,
    service=AssingmentsAsyncService,
    mongo=MongoResourceConfig(
        indexes=[
            MongoIndexOptions(name="coverage_item_1", keys=[("coverage_item", 1)]),
            MongoIndexOptions(name="planning_item_1", keys=[("planning_item", 1)]),
            MongoIndexOptions(name="published_state_1", keys=[("published_state", 1)]),
        ],
    ),
    elastic=ElasticResourceConfig(),
)
