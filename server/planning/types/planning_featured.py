from datetime import datetime
from pydantic import Field
from typing import Annotated

from .base import BasePlanningModel
from superdesk.utc import utcnow
from superdesk.core.resources import fields
from superdesk.core.resources.validators import validate_data_relation_async


class PlanningFeaturedResourceModel(BasePlanningModel):
    date: datetime = Field(default_factory=utcnow)
    items: list[str] = Field(default_factory=list)
    tz: str | None = None
    posted: bool = False
    last_posted_time: datetime | None = None
    last_posted_by: Annotated[fields.ObjectId | None, validate_data_relation_async("users")] = None
    firstcreated: datetime = Field(default_factory=utcnow)
    versioncreated: datetime = Field(default_factory=utcnow)
    item_type: str = Field(
        alias="type", default="planning_featured", description="Item type used by superdesk publishing"
    )
