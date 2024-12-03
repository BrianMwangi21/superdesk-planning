from superdesk.core.resources import (
    ResourceConfig,
    MongoIndexOptions,
    MongoResourceConfig,
    ElasticResourceConfig,
)

from planning.types import AssignmentResourceModel
from .service import AssignmentsAsyncService

assignments_resource_config = ResourceConfig(
    name="assignments",
    data_class=AssignmentResourceModel,
    service=AssignmentsAsyncService,
    etag_ignore_fields=["planning", "published_state", "published_at"],
    mongo=MongoResourceConfig(
        indexes=[
            MongoIndexOptions(
                name="coverage_item_1",
                keys=[("coverage_item", 1)],
                unique=False,
            ),
            MongoIndexOptions(
                name="planning_item_1",
                keys=[("planning_item", 1)],
                unique=False,
            ),
            MongoIndexOptions(
                name="published_state_1",
                keys=[("published_state", 1)],
                unique=False,
            ),
        ],
    ),
    elastic=ElasticResourceConfig(),
)
