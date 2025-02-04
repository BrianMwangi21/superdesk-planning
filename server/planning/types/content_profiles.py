# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
#  Copyright 2023 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

from typing import TypedDict, Dict, List


class ContentFieldSchema(TypedDict, total=False):
    multilingual: bool
    field_type: str
    planning_auto_publish: bool  # Only available in ``related_plannings`` field
    cancel_plan_with_event: bool  # Only available in ``related_plannings`` field
    vocabularies: List[str]  # Only available in ``custom_vocabularies`` field


class ContentFieldEditor(TypedDict):
    enabled: bool


class ContentProfile(TypedDict):
    _id: str
    name: str
    schema: Dict[str, ContentFieldSchema]
    editor: Dict[str, ContentFieldEditor]
