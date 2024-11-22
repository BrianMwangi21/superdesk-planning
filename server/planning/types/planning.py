from pydantic import Field
from datetime import datetime
from typing import Annotated, Any

from content_api.items.model import CVItem, Place

from superdesk.utc import utcnow
from superdesk.core.resources import fields, dataclass
from superdesk.core.resources.validators import validate_data_relation_async

from .event import Translation
from .base import BasePlanningModel
from .common import RelatedEvent, Subject, PlanningCoverage
from .enums import PostStates, UpdateMethods, WorkflowState


@dataclass
class Flags:
    marked_for_not_publication: bool = False
    overide_auto_assign_to_workflow: bool = False


class PlanningResourceModel(BasePlanningModel):
    guid: fields.Keyword
    unique_id: fields.Keyword | None = None

    firstcreated: datetime = Field(default_factory=utcnow)
    versioncreated: datetime = Field(default_factory=utcnow)

    # Ingest Details
    ingest_provider: Annotated[fields.ObjectId, validate_data_relation_async("ingest_providers")] | None = None
    source: fields.Keyword | None = None
    original_source: fields.Keyword | None = None
    ingest_provider_sequence: fields.Keyword | None = None
    ingest_firstcreated: datetime = Field(default_factory=utcnow)
    ingest_versioncreated: datetime = Field(default_factory=utcnow)

    # Agenda Item details
    agendas: list[Annotated[str, validate_data_relation_async("agenda")]] = Field(default_factory=list)
    related_events: list[RelatedEvent] = Field(default_factory=list)
    recurrence_id: fields.Keyword | None = None
    planning_recurrence_id: fields.Keyword | None = None

    # Planning Details
    # NewsML-G2 Event properties See IPTC-G2-Implementation_Guide 16
    # Planning Item Metadata - See IPTC-G2-Implementation_Guide 16.1
    item_class: str = Field(default="plinat:newscoverage")
    ednote: str | None = None
    description_text: str | None = None
    internal_note: str | None = None
    anpa_category: list[CVItem] = Field(default_factory=list)
    subject: list[Subject] = Field(default_factory=list)
    genre: list[CVItem] = Field(default_factory=list)
    company_codes: list[CVItem] = Field(default_factory=list)

    # Content Metadata - See IPTC-G2-Implementation_Guide 16.2
    language: fields.Keyword | None = None
    languages: list[fields.Keyword] = Field(default_factory=list)
    translations: Annotated[list[Translation], fields.nested_list()] = Field(default_factory=list)
    abstract: str | None = None
    headline: str | None = None
    slugline: str | None = None
    keywords: list[str] = Field(default_factory=list)
    word_count: int | None = None
    priority: int | None = None
    urgency: int | None = None
    profile: str | None = None

    # These next two are for spiking/unspiking and purging of planning/agenda items
    state: WorkflowState = WorkflowState.DRAFT
    expiry: datetime | None = None
    expired: bool = False
    featured: bool = False
    lock_user: Annotated[fields.ObjectId, validate_data_relation_async("users")] | None = None
    lock_time: datetime | None = None
    lock_session: Annotated[fields.ObjectId, validate_data_relation_async("users")] | None = None
    lock_action: fields.Keyword | None = None
    coverages: list[PlanningCoverage] = Field(default_factory=list)

    # field to sync coverage scheduled information
    # to be used for sorting/filtering on scheduled
    planning_schedule: Annotated[list[dict[str, Any]], fields.nested_list()] = Field(
        default_factory=list, alias="_planning_schedule"
    )

    # field to sync scheduled_updates scheduled information
    # to be used for sorting/filtering on scheduled
    updates_schedule: Annotated[list[dict[str, Any]], fields.nested_list()] = Field(
        default_factory=list, alias="updates_schedule"
    )
    planning_date: datetime
    flags: Flags = Field(default_factory=Flags)
    pubstatus: PostStates | None = None
    revert_state: WorkflowState | None = None

    # Item type used by superdesk publishing
    item_type: Annotated[fields.Keyword, Field(alias="type")] = "planning"
    place: list[Place] = Field(default_factory=list)
    name: str | None = None
    files: list[Annotated[str, validate_data_relation_async("planning_files")]] = Field(default_factory=list)

    # Reason (if any) for the current state (cancelled, postponed, rescheduled)
    state_reason: str | None = None
    time_to_be_confirmed: bool = Field(default=False, alias="_time_to_be_confirmed")
    extra: Annotated[dict[str, Any], fields.elastic_mapping({"type": "object", "dynamic": True})] = Field(
        default_factory=dict
    )

    versionposted: datetime | None = None
    update_method: UpdateMethods | None = None

    # TODO-ASYNC: check why do we have `type` and `_type`
    _type: str | None = None
