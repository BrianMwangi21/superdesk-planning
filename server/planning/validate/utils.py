from copy import deepcopy
import logging
from typing import Any

from superdesk import get_resource_service
from superdesk.metadata.item import ITEM_TYPE
from planning.validate.planning_validate import SchemaValidator
from planning.content_profiles.utils import get_enabled_fields

logger = logging.getLogger(__name__)
REQUIRED_ERROR = "{} is a required field"


def get_validator_schema(schema):
    """Get schema for given data that will work with validator.

    - if field is required without minlength set make sure it's not empty
    - if there are keys with None value - remove them

    :param schema
    """
    validator_schema = {key: val for key, val in schema.items() if val is not None}

    if validator_schema.get("required") and not validator_schema.get("minlength"):
        validator_schema.setdefault("empty", False)

    return validator_schema


def get_filtered_validator_schema(validator, validate_on_post: bool):
    """Get schema for a given validator, excluding fields with None values,
    and only include fields that are in enabled_fields."""

    enabled_fields = get_enabled_fields(validator)
    return {
        field: get_validator_schema(field_schema)
        for field, field_schema in validator["schema"].items()
        if field in enabled_fields and field_schema and field_schema.get("validate_on_post", False) == validate_on_post
    }


async def get_validator(doc: dict[str, Any]):
    """Get validators from planning types service."""
    return get_resource_service("planning_types").find_one(req=None, name=doc[ITEM_TYPE])


async def validate_doc(doc: dict[str, Any]):
    validator = await get_validator(doc)

    if validator is None:
        logger.warn("Validator was not found for type:{}".format(doc[ITEM_TYPE]))
        return []

    validation_schema = get_filtered_validator_schema(validator, doc.get("validate_on_post", False))

    v = SchemaValidator()
    v.allow_unknown = True

    try:
        v.validate(doc["validate"], validation_schema)
    except TypeError as e:
        logger.exception('Invalid validator schema value "%s" for ' % str(e))

    error_list = v.errors
    response = []
    for field in error_list:
        error = error_list[field]

        # If error is a list, only return the first error
        if isinstance(error, list):
            error = error[0]

        if error == "empty values not allowed" or error == "required field":
            response.append(REQUIRED_ERROR.format(field.upper()))
        else:
            response.append("{} {}".format(field.upper(), error))

    return response


async def validate_docs(docs: list[dict[str, Any]]):
    """
    Validate a list of documents asynchronously and returns a list of
    validation errors
    """
    for doc in docs:
        test_doc = deepcopy(doc)
        doc["errors"] = await validate_doc(test_doc)

    return [doc["errors"] for doc in docs]
