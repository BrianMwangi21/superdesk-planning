from planning.types import EventResourceModel
from planning.core.service import BasePlanningAsyncService


class EventsAsyncService(BasePlanningAsyncService[EventResourceModel]):
    resource_name = "events"
