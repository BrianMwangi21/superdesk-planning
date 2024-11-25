from planning.types import EventResourceModel
from planning.core.service import PlanningAsyncResourceService


class EventsAsyncService(PlanningAsyncResourceService[EventResourceModel]):
    resource_name = "events"
