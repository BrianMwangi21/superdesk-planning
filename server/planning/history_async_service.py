# This file is part of Superdesk.
#
# Copyright 2013, 2014 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Superdesk Files"""

from copy import deepcopy
from bson import ObjectId

from superdesk.core import get_current_app
from superdesk.core.resources import AsyncResourceService
from superdesk.resource_fields import ID_FIELD
from .item_lock import LOCK_ACTION, LOCK_USER, LOCK_TIME, LOCK_SESSION
from superdesk.metadata.item import ITEM_TYPE


fields_to_remove = [
    "_id",
    "_etag",
    "_current_version",
    "_updated",
    "_created",
    "_links",
    "version_creator",
    "guid",
    LOCK_ACTION,
    LOCK_USER,
    LOCK_TIME,
    LOCK_SESSION,
    "planning_ids",
    "_updates_schedule",
    "_planning_schedule",
    "_planning_date",
    "_reschedule_from_schedule",
    "versioncreated",
]


class HistoryAsyncService(AsyncResourceService):
    """Provide common async methods for tracking history of Creation, Updates and Spiking to collections"""

    async def on_item_created(self, items, operation=None):
        for item in items:
            if not item.get("duplicate_from"):
                await self._save_history(
                    {ID_FIELD: ObjectId(item[ID_FIELD]) if ObjectId.is_valid(item[ID_FIELD]) else str(item[ID_FIELD])},
                    deepcopy(item),
                    operation or "create",
                )

    async def on_item_updated(self, updates, original, operation=None):
        item = deepcopy(original)
        if list(item.keys()) == ["_id"]:
            diff = updates
        else:
            diff = self._changes(original, updates)
            if updates:
                item.update(updates)

        await self._save_history(item, diff, operation or "edited")

    async def on_spike(self, updates, original):
        await self.on_item_updated(updates, original, "spiked")

    async def on_unspike(self, updates, original):
        await self.on_item_updated(updates, original, "unspiked")

    async def on_cancel(self, updates, original):
        operation = "events_cancel" if original.get(ITEM_TYPE) == "event" else "planning_cancel"
        await self.on_item_updated(updates, original, operation)

    async def on_reschedule(self, updates, original):
        await self.on_item_updated(updates, original, "reschedule")

    async def on_reschedule_from(self, item):
        new_item = deepcopy(item)
        await self._save_history({ID_FIELD: str(item[ID_FIELD])}, new_item, "reschedule_from")

    async def on_postpone(self, updates, original):
        await self.on_item_updated(updates, original, "postpone")

    async def get_user_id(self):
        user = get_current_app().get_current_user_dict()
        if user:
            return user.get("_id")

    async def _changes(self, original, updates):
        """
        Given the original record and the updates calculate what has changed and what is new

        :param original:
        :param updates:
        :return: dictionary of what was changed and what was added
        """
        original_keys = set(original.keys())
        updates_keys = set(updates.keys())
        intersect_keys = original_keys.intersection(updates_keys)
        modified = {o: updates[o] for o in intersect_keys if original[o] != updates[o]}
        added_keys = updates_keys - original_keys
        added = {a: updates[a] for a in added_keys}
        modified.update(added)
        return await self._remove_unwanted_fields(modified)

    async def _remove_unwanted_fields(self, update):
        if update:
            update_copy = deepcopy(update)
            for field in fields_to_remove:
                update_copy.pop(field, None)

            return update_copy
        return update

    async def _save_history(self, item, update, operation):
        raise NotImplementedError()
