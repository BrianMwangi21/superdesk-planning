from pydantic import Field
from datetime import datetime
from typing import Annotated, Any

from content_api.items.model import CVItem, ContentAPIItem, Place

from superdesk.utc import utcnow
from superdesk.core.resources import fields, dataclass
from superdesk.core.resources.validators import validate_data_relation_async

from .base import BasePlanningModel
from .event_dates import EventDates, OccurStatus
from .enums import ContentState, PostStates, UpdateMethods, WorkflowState
from .common import CoverageStatus, KeywordQCodeName, PlanningSchedule, RelationshipItem, Subject


class NameAnalyzed(str, fields.CustomStringField):
    elastic_mapping = {
        "type": "keyword",
        "fields": {
            "analyzed": {"type": "text", "analyzer": "html_field_analyzer"},
        },
    }


class SlugLine(str, fields.CustomStringField):
    elastic_mapping = {
        "type": "string",
        "fielddata": True,
        "fields": {
            "phrase": {
                "type": "string",
                "analyzer": "phrase_prefix_analyzer",
                "fielddata": True,
            },
            "keyword": {
                "type": "keyword",
            },
            "text": {"type": "string", "analyzer": "html_field_analyzer"},
        },
    }


@dataclass
class EventLocation:
    name: fields.TextWithKeyword
    qcode: fields.Keyword | None = None
    address: Annotated[dict[str, None] | None, fields.dynamic_mapping()] = None
    geo: str | None = None
    location: fields.Geopoint | None = None


# HACK: ``index``. Temporal place for this indexes workaround
CoveragesIndex = Annotated[
    list,
    fields.elastic_mapping(
        {
            "type": "nested",
            "properties": {
                "planning": {
                    "type": "object",
                    "dynamic": False,
                    "properties": {
                        "slugline": {
                            "type": "string",
                            "fields": {
                                "phrase": {
                                    "type": "string",
                                    "analyzer": "phrase_prefix_analyzer",
                                    "search_analyzer": "phrase_prefix_analyzer",
                                }
                            },
                        },
                    },
                }
            },
        },
    ),
]

RelatedEvents = Annotated[
    list[dict[str, Any]],
    fields.elastic_mapping(
        {
            "type": "nested",
            "properties": {
                "_id": "keyword",
                "recurrence_id": "keyword",
                "link_type": "keyword",
            },
        }
    ),
]
# HACK: end


@dataclass
class Translation:
    field: fields.Keyword | None = None
    language: fields.Keyword | None = None
    value: SlugLine | None = None


@dataclass
class Coverage:
    coverage_id: str
    g2_content_type: str
    news_coverage_status: str
    scheduled: datetime
    desk: str | None = None
    user: Annotated[str | None, validate_data_relation_async("users")] = None
    language: str | None = None
    genre: str | None = None
    slugline: str | None = None
    headline: str | None = None
    ednote: str | None = None
    internal_note: str | None = None
    priority: int | None = None


@dataclass
class EmbeddedPlanning:
    planning_id: Annotated[str, validate_data_relation_async("planning")]
    update_method: UpdateMethods | None = None
    coverages: list[Coverage] | None = Field(default_factory=list)


class EventResourceModel(BasePlanningModel):
    guid: fields.Keyword
    unique_id: int | None = None
    unique_name: fields.Keyword | None = None
    version: int | None = None
    ingest_id: fields.Keyword | None = None
    recurrence_id: fields.Keyword | None = None

    # This is used when recurring series are split
    previous_recurrence_id: fields.Keyword | None = None

    # TODO-ASYNC: consider moving these two to the base model if it used everywhere
    firstcreated: datetime = Field(default_factory=utcnow)
    versioncreated: datetime = Field(default_factory=utcnow)

    # Ingest Details
    ingest_provider: Annotated[fields.ObjectId, validate_data_relation_async("ingest_providers")] = None
    # The value is copied from the ingest_providers vocabulary
    source: fields.Keyword | None = None
    # This value is extracted from the ingest
    original_source: fields.Keyword | None = None

    ingest_provider_sequence: fields.Keyword | None = None
    ingest_firstcreated: datetime = Field(default_factory=utcnow)
    ingest_versioncreated: datetime = Field(default_factory=utcnow)
    event_created: datetime = Field(default_factory=utcnow)
    event_lastmodified: datetime = Field(default_factory=utcnow)

    # Event Details
    # NewsML-G2 Event properties See IPTC-G2-Implementation_Guide 15.2
    name: str
    definition_short: str | None = None
    definition_long: str | None = None
    internal_note: str | None = None
    registration_details: str | None = None
    invitation_details: str | None = None
    accreditation_info: str | None = None
    accreditation_deadline: datetime | None = None

    # Reference can be used to hold for example a court case reference number
    reference: str | None = None
    anpa_category: list[CVItem] = Field(default_factory=list)
    files: Annotated[list[fields.ObjectId], validate_data_relation_async("events_files")] = Field(default_factory=list)

    relationships: RelationshipItem | None = None
    links: list[str] = Field(default_factory=list)
    priority: int | None = None

    # NewsML-G2 Event properties See IPTC-G2-Implementation_Guide 15.4.3
    dates: EventDates | None = None

    # This is an extra field so that we can sort in the combined view of events and planning.
    # It will store the dates.start of the event.
    planning_schedule: Annotated[list[PlanningSchedule], fields.nested_list()] = Field(
        alias="_planning_schedule", default_factory=list
    )

    occur_status: OccurStatus | None = None
    news_coverage_status: CoverageStatus | None = None
    registration: str | None = None
    access_status: KeywordQCodeName | None = None

    # Content metadata
    subject: list[Subject | None] = Field(default_factory=list)
    slugline: SlugLine | None = None

    # Item metadata
    location: list[EventLocation | None] = Field(default_factory=list)
    participant: list[KeywordQCodeName | None] = Field(default_factory=list)
    participant_requirement: list[KeywordQCodeName | None] = Field(default_factory=list)
    organizer: list[KeywordQCodeName | None] = Field(default_factory=list)
    event_contact_info: Annotated[list[fields.ObjectId], validate_data_relation_async("contacts")]
    language: fields.Keyword | None = None
    languages: list[fields.Keyword] = Field(default_factory=list)

    # These next two are for spiking/unspiking and purging events
    state: WorkflowState = WorkflowState.DRAFT
    expiry: datetime | None = None
    expired: bool = False

    # says if the event is for internal usage or posted
    pubstatus: PostStates | None = None
    lock_user: Annotated[fields.ObjectId, validate_data_relation_async("users")]
    lock_time: datetime
    lock_session: Annotated[fields.ObjectId, validate_data_relation_async("users")]
    lock_action: fields.Keyword | None = None

    # The update method used for recurring events
    update_method: UpdateMethods | None = None

    # Item type used by superdesk publishing
    item_type: Annotated[fields.Keyword, Field(alias="type")] = "event"

    # Named Calendars
    calendars: list[KeywordQCodeName] | None = None

    # The previous state the item was in before for example being spiked,
    # when un-spiked it will revert to this state
    revert_state: ContentState | None = None

    # Used when duplicating/rescheduling of Events
    duplicate_from: Annotated[str, validate_data_relation_async("events")] | None = None
    duplicate_to: list[Annotated[str, validate_data_relation_async("events")]] = Field(default_factory=list)

    reschedule_from: Annotated[str, validate_data_relation_async("events")] | None = None
    reschedule_to: Annotated[str, validate_data_relation_async("events")] | None = None
    reschedule_from_schedule: datetime | None = Field(default=None, alias="_reschedule_from_schedule")
    place: list[Place] = Field(default_factory=list)
    ednote: Annotated[str, fields.elastic_mapping({"analyzer": "html_field_analyzer"})] | None = None

    # Reason (if any) for the current state (cancelled, postponed, rescheduled)
    state_reason: str | None = None

    # Datetime when a particular action (postpone, reschedule, cancel) took place
    actioned_date: datetime | None = None
    completed: bool = False
    time_to_be_confirmed: bool = Field(default=False, alias="_time_to_be_confirmed")

    # This is used if an Event is created from a Planning Item
    # So that we can link the Planning item to this Event upon creation
    planning_item: Annotated[str | None, validate_data_relation_async("planning")] = Field(
        default=None, alias="_planning_item"
    )

    # This is used when event creation was based on `events_template`
    template: Annotated[str | None, validate_data_relation_async("events_template")] = None

    # This is used when enhancing fetch items to add ids of associated Planning items
    planning_ids: list[Annotated[str, validate_data_relation_async("planning")]] = Field(default_factory=list)

    # HACK: ``coverages`` and ``related_events``
    # adds these fields to the Events elastic type. So when we're in the Events & Planning filter,
    # we can send a query to both Event & Planning index without modifying the query.
    # Otherwise elastic will raise an exception stating the field doesn't exist on the index
    coverages: CoveragesIndex | None = None
    related_events: RelatedEvents | None = None
    # HACK: end. We'll try to move this hacks somewhere else

    extra: Annotated[dict[str, Any], fields.elastic_mapping({"type": "object", "dynamic": True})] = Field(
        default_factory=dict
    )
    translations: Annotated[list[Translation], fields.nested_list()]

    # This is used from the EmbeddedCoverage form in the Event editor
    # This list is NOT stored with the Event
    embedded_planning: Annotated[list[EmbeddedPlanning], fields.not_indexed] = Field(default_factory=list)

    # This is used to create new planning items from the event editor
    # TODO-ASYNC: consider adding proper types instead of a dynamic dict
    associated_plannings: Annotated[
        list[dict[str, Any]], fields.elastic_mapping({"type": "object", "dynamic": True})
    ] = Field(default_factory=list)

    related_items: list[ContentAPIItem] = Field(default_factory=list)
    failed_planned_ids: list[str] = Field(default_factory=list)

    # TODO-ASYNC: check why do we have `type` and `_type`
    _type: str | None = None
