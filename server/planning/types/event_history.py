from typing import Annotated, Any

from superdesk.core.resources import ResourceModelWithObjectId, fields
from superdesk.core.resources.validators import validate_data_relation_async


class EventsHistoryResourceModel(ResourceModelWithObjectId):
    event_id: Annotated[fields.ObjectId, validate_data_relation_async("events")]
    user_id: Annotated[fields.ObjectId, validate_data_relation_async("users")]
    operation: str
    update: dict[str, Any] | None = None
