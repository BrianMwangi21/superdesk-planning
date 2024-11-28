from pydantic import Field
from typing import Annotated
from datetime import datetime

from superdesk.utc import utcnow
from superdesk.core.resources import fields, dataclass
from superdesk.core.resources.validators import validate_data_relation_async

from .base import BasePlanningModel
from .common import PlanningCoverage
from .enums import AssignmentPublishedState, AssignmentWorkflowState


@dataclass
class CoverageProvider:
    qcode: fields.Keyword | None = None
    name: fields.Keyword | None = None
    contact_type: fields.Keyword | None = None


@dataclass
class AssignedTo:
    desk: fields.Keyword | None = None
    user: fields.Keyword | None = None
    contact: fields.Keyword | None = None
    assignor_desk: fields.Keyword | None = None
    assignor_user: fields.Keyword | None = None
    assigned_date_desk: datetime | None = None
    assigned_date_user: datetime | None = None
    state: AssignmentWorkflowState | None = None
    revert_state: AssignmentWorkflowState | None = None
    coverage_provider: CoverageProvider | None = None


class AssignmentResourceModel(BasePlanningModel):
    firstcreated: datetime = Field(default_factory=utcnow)
    versioncreated: datetime = Field(default_factory=utcnow)

    item_type: Annotated[fields.Keyword, Field(alias="type")] = "assignment"
    priority: int | None = None
    coverage_item: fields.Keyword | None = None
    planning_item: Annotated[str, validate_data_relation_async("planning")]
    scheduled_update_id: fields.Keyword | None = None

    lock_user: Annotated[fields.ObjectId, validate_data_relation_async("users")] | None = None
    lock_time: datetime | None = None
    lock_session: Annotated[fields.ObjectId, validate_data_relation_async("users")] | None = None
    lock_action: fields.Keyword | None = None

    assigned_to: AssignedTo | None = None
    planning: PlanningCoverage | None = None

    name: str | None = None
    description_text: str | None = None
    accepted: bool = False
    to_delete: bool = Field(default=False, alias="_to_delete")

    published_at: datetime | None = None
    published_state: AssignmentPublishedState | None = None
