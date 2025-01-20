from datetime import datetime
from pydantic import Field
from typing import Annotated

from .base import BasePlanningModel
from superdesk.utc import utcnow
from superdesk.core.resources import fields
from superdesk.core.resources.validators import validate_data_relation_async


class PlanningFeaturedResourceModel(BasePlanningModel):
    date: datetime = Field(default_factory=utcnow)
    items: list = Field(default_factory=list)
    tz: str
    posted: bool
    last_posted_time: datetime = Field(default_factory=utcnow)
    last_posted_by: Annotated[fields.ObjectId, validate_data_relation_async("users")]
    firstcreated: datetime = Field(default_factory=utcnow)
    versioncreated: datetime = Field(default_factory=utcnow)
    item_type: str = Field(
        alias="type", default="planning_featured", description="Item type used by superdesk publishing"
    )
