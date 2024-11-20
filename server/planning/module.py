from superdesk.core.module import Module
from planning.events import events_resource_config


module = Module(
    "planning",
    resources=[events_resource_config],
)
