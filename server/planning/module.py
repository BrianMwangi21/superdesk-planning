from superdesk.core.module import Module
from planning.events import events_resource_config
from planning.planning import planning_resource_config
from planning.assignments import assignments_resource_config
from planning.published import published_resource_config


module = Module(
    "planning",
    resources=[
        events_resource_config,
        planning_resource_config,
        assignments_resource_config,
        published_resource_config,
    ],
)
