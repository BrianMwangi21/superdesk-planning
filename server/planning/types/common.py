from datetime import date, datetime
from typing import Any, Annotated

from pydantic import Field
from superdesk.core.resources import dataclass, fields
from superdesk.core.resources.validators import validate_data_relation_async


class NameAnalyzed(str, fields.CustomStringField):
    elastic_mapping = {
        "type": "keyword",
        "fields": {
            "analyzed": {"type": "text", "analyzer": "html_field_analyzer"},
        },
    }


Translations = Annotated[
    dict[str, Any],
    fields.elastic_mapping(
        {
            "type": "object",
            "dynamic": False,
            "properties": {
                "name": {
                    "type": "object",
                    "dynamic": True,
                }
            },
        }
    ),
]


@dataclass
class RelationshipItem:
    broader: str | None = None
    narrower: str | None = None
    related: str | None = None


@dataclass
class PlanningSchedule:
    scheduled: date


@dataclass
class CoverageStatus:
    qcode: str
    name: str


@dataclass
class KeywordQCodeName:
    qcode: fields.Keyword
    name: fields.Keyword


@dataclass
class Subject:
    qcode: fields.Keyword
    name: NameAnalyzed
    scheme: fields.Keyword
    translations: Translations | None = None


SubjectListType = Annotated[list[Subject], fields.nested_list(include_in_parent=True)]


@dataclass
class Place:
    scheme: fields.Keyword | None = None
    qcode: fields.Keyword | None = None
    code: fields.Keyword | None = None
    name: fields.Keyword | None = None
    locality: fields.Keyword | None = None
    state: fields.Keyword | None = None
    country: fields.Keyword | None = None
    world_region: fields.Keyword | None = None
    locality_code: fields.Keyword | None = None
    state_code: fields.Keyword | None = None
    country_code: fields.Keyword | None = None
    world_region_code: fields.Keyword | None = None
    feature_class: fields.Keyword | None = None
    location: fields.Geopoint | None = None
    rel: fields.Keyword | None = None


@dataclass
class RelatedEvent:
    id: Annotated[str, validate_data_relation_async("events")] = Field(alias="_id")
    recurrence_id: str | None = None
    link_type: str | None = None


@dataclass
class PlanningCoverage:
    coverage_id: str
    planning: dict[str, Any]
    assigned_to: dict[str, Any]
    original_creator: str | None = None


class LockFieldsMixin:
    lock_user: Annotated[fields.ObjectId, validate_data_relation_async("users")] | None = None
    lock_time: datetime | None = None
    lock_session: Annotated[fields.ObjectId, validate_data_relation_async("users")] | None = None
    lock_action: fields.Keyword | None = None
