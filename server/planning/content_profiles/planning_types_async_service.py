# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2021 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

from typing import Any
from copy import deepcopy

from superdesk.core.resources import AsyncResourceService
from superdesk.core.types import SearchRequest, ProjectedFieldArg
from superdesk.utils import ListCursor

from planning.common import planning_link_updates_to_coverage, get_config_event_related_item_search_provider_name
from planning.types import PlanningTypesResourceModel
from .profiles import DEFAULT_PROFILES


class PlanningTypesAsyncService(AsyncResourceService[PlanningTypesResourceModel]):
    """Planning types async service

    Provide a service that returns what fields should be shown in the edit forms in planning, in the edit dictionary.
    Also provide a schema to allow the client to validate the values entered in the forms.
    Entries can be overridden by providing alternates in the planning_types mongo collection.
    """

    async def find_one(
        self,
        req: SearchRequest | None = None,
        projection: ProjectedFieldArg | None = None,
        use_mongo: bool = False,
        version: int | None = None,
        **lookup,
    ) -> PlanningTypesResourceModel | None:
        try:
            if req is None:
                planning_type = await super().find_one(
                    projection=projection, use_mongo=use_mongo, version=version, **lookup
                )
            else:
                planning_type = await self.find_one(req)

            # lookup name from either **lookup of planning_item(if lookup has only '_id')
            lookup_name = lookup.get("name")
            if not lookup_name and planning_type:
                lookup_name = planning_type.to_dict().get("name")

            default_planning_type = deepcopy(
                next(
                    (ptype for ptype in DEFAULT_PROFILES if ptype.get("name") == lookup_name),
                    {},
                )
            )
            if not planning_type:
                await self._remove_unsupported_fields(default_planning_type)
                return PlanningTypesResourceModel(**default_planning_type)

            await self.merge_planning_type(planning_type.to_dict(), default_planning_type)
            return planning_type
        except IndexError:
            return None

    async def get(self, req, lookup) -> ListCursor:
        cursor = await super().search(lookup)
        planning_types = await cursor.to_list_raw()
        merged_planning_types = []

        for default_planning_type in deepcopy(DEFAULT_PROFILES):
            planning_type = next(
                (p for p in planning_types if p.get("name") == default_planning_type.get("name")),
                None,
            )

            # If nothing is defined in database for this planning_type, use default
            if planning_type is None:
                await self._remove_unsupported_fields(default_planning_type)
                merged_planning_types.append(default_planning_type)
            else:
                await self.merge_planning_type(planning_type, default_planning_type)
                merged_planning_types.append(planning_type)

        return ListCursor(merged_planning_types)

    async def merge_planning_type(self, planning_type: dict[str, Any], default_planning_type: dict[str, Any]):
        # Update schema fields with database schema fields
        default_type = {"schema": {}, "editor": {}}
        updated_planning_type = deepcopy(default_planning_type or default_type)

        updated_planning_type.setdefault("groups", {})
        updated_planning_type["groups"].update(planning_type.get("groups", {}))

        if planning_type["name"] == "advanced_search":
            updated_planning_type["schema"].update(planning_type.get("schema", {}))
            updated_planning_type["editor"]["event"].update((planning_type.get("editor") or {}).get("event"))
            updated_planning_type["editor"]["planning"].update((planning_type.get("editor") or {}).get("planning"))
            updated_planning_type["editor"]["combined"].update((planning_type.get("editor") or {}).get("combined"))
        elif planning_type["name"] in ["event", "planning", "coverage"]:
            for config_type in ["editor", "schema"]:
                planning_type.setdefault(config_type, {})
                for field, options in updated_planning_type[config_type].items():
                    # If this field is none, then it is of type `schema.NoneField()`
                    # no need to copy any schema
                    if updated_planning_type[config_type][field]:
                        updated_planning_type[config_type][field].update(planning_type[config_type].get(field) or {})
        else:
            updated_planning_type["editor"].update(planning_type.get("editor", {}))
            updated_planning_type["schema"].update(planning_type.get("schema", {}))

        planning_type["schema"] = updated_planning_type["schema"]
        planning_type["editor"] = updated_planning_type["editor"]
        planning_type["groups"] = updated_planning_type["groups"]
        await self._remove_unsupported_fields(planning_type)

    async def _remove_unsupported_fields(self, planning_type: dict[str, Any]):
        # Disable Event ``related_items`` field
        # if ``EVENT_RELATED_ITEM_SEARCH_PROVIDER_NAME`` config is not set
        if planning_type.get("name") == "event" and not get_config_event_related_item_search_provider_name():
            planning_type["editor"].pop("related_items", None)
            planning_type["schema"].pop("related_items", None)

        # Disable Coverage ``no_content_linking`` field
        # if ``PLANNING_LINK_UPDATES_TO_COVERAGES`` config is not ``True``
        if planning_type.get("name") == "coverage" and not planning_link_updates_to_coverage():
            planning_type["editor"].pop("no_content_linking", None)
            planning_type["schema"].pop("no_content_linking", None)
