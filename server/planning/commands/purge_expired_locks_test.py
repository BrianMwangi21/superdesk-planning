# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2024 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

from typing import List, Tuple, Union
from datetime import timedelta
from bson import ObjectId

from superdesk.utc import utcnow
from planning.tests import TestCase
from planning.events import EventsAsyncService
from planning.planning import PlanningAsyncService
from planning.assignments import AssingmentsAsyncService
from .purge_expired_locks import purge_expired_locks_handler

now = utcnow()
assignment_1_id = ObjectId()
assignment_2_id = ObjectId()


# TODO: Add Assignments
class PurgeExpiredLocksTest(TestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.app_config.update({"MODULES": ["planning.module"]})
        self.service_mapping = {
            "events": EventsAsyncService(),
            "planning": PlanningAsyncService(),
            "assignments": AssingmentsAsyncService(),
        }

        await self.insert(
            "events",
            [
                {
                    "_id": "active_event_1",
                    "dates": {"start": now, "end": now + timedelta(days=1)},
                    "lock_user": "user1",
                    "lock_session": "session1",
                    "lock_time": now - timedelta(hours=23),
                    "lock_action": "edit",
                },
                {
                    "_id": "expired_event_1",
                    "dates": {"start": now, "end": now + timedelta(days=1)},
                    "lock_user": "user2",
                    "lock_session": "session2",
                    "lock_time": now - timedelta(hours=25),
                    "lock_action": "edit",
                },
            ],
        )
        await self.insert(
            "planning",
            [
                {
                    "_id": "active_plan_1",
                    "planning_date": now,
                    "lock_user": "user3",
                    "lock_session": "session3",
                    "lock_time": now - timedelta(hours=23),
                    "lock_action": "edit",
                },
                {
                    "_id": "expired_plan_1",
                    "planning_date": now,
                    "lock_user": "user4",
                    "lock_session": "session4",
                    "lock_time": now - timedelta(hours=25),
                    "lock_action": "edit",
                },
            ],
        )
        await self.insert(
            "assignments",
            [
                {
                    "_id": assignment_1_id,
                    "lock_user": "user5",
                    "lock_session": "session5",
                    "lock_time": now - timedelta(hours=23),
                    "lock_action": "edit",
                },
                {
                    "_id": assignment_2_id,
                    "lock_user": "user6",
                    "lock_session": "session6",
                    "lock_time": now - timedelta(hours=25),
                    "lock_action": "edit",
                },
            ],
        )
        await self.assertLockState(
            [
                ("events", "active_event_1", True),
                ("events", "expired_event_1", True),
                ("planning", "active_plan_1", True),
                ("planning", "expired_plan_1", True),
                ("assignments", assignment_1_id, True),
                ("assignments", assignment_2_id, True),
            ]
        )

    async def insert(self, item_type, items):
        try:
            service = self.service_mapping[item_type]
        except KeyError:
            raise ValueError(f"Invalid item_type: {item_type}")
        await service.create(items)

    async def assertLockState(self, item_tests: List[Tuple[str, Union[str, ObjectId], bool]]):
        for resource, item_id, is_locked in item_tests:
            try:
                service = self.service_mapping[resource]
            except KeyError:
                raise ValueError(f"Invalid resource: {resource}")

            item = await service.find_by_id(item_id)
            if not item:
                raise AssertionError(f"{resource} item with ID {item_id} not found")

            if is_locked:
                self.assertIsNotNone(item.get("lock_user"), f"{resource} item {item_id} is NOT locked, item={item}")
                self.assertIsNotNone(item.get("lock_session"), f"{resource} item {item_id} is NOT locked, item={item}")
                self.assertIsNotNone(item.get("lock_time"), f"{resource} item {item_id} is NOT locked, item={item}")
                self.assertIsNotNone(item.get("lock_action"), f"{resource} item {item_id} is NOT locked, item={item}")
            else:
                self.assertIsNone(item.get("lock_user"), f"{resource} item {item_id} is locked, item={item}")
                self.assertIsNone(item.get("lock_session"), f"{resource} item {item_id} is locked, item={item}")
                self.assertIsNone(item.get("lock_time"), f"{resource} item {item_id} is locked, item={item}")
                self.assertIsNone(item.get("lock_action"), f"{resource} item {item_id} is locked, item={item}")

    async def test_invalid_resource(self):
        with self.assertRaises(ValueError):
            await purge_expired_locks_handler("blah")

    async def test_purge_event_locks(self):
        async with self.app.app_context():
            await purge_expired_locks_handler("events")
            await self.assertLockState(
                [
                    ("events", "active_event_1", True),
                    ("events", "expired_event_1", False),
                    ("planning", "active_plan_1", True),
                    ("planning", "expired_plan_1", True),
                    ("assignments", assignment_1_id, True),
                    ("assignments", assignment_2_id, True),
                ]
            )

    async def test_purge_planning_locks(self):
        async with self.app.app_context():
            await purge_expired_locks_handler("planning")
            await self.assertLockState(
                [
                    ("events", "active_event_1", True),
                    ("events", "expired_event_1", True),
                    ("planning", "active_plan_1", True),
                    ("planning", "expired_plan_1", False),
                    ("assignments", assignment_1_id, True),
                    ("assignments", assignment_2_id, True),
                ]
            )

    async def test_purge_assignment_locks(self):
        async with self.app.app_context():
            await purge_expired_locks_handler("assignments")
            await self.assertLockState(
                [
                    ("events", "active_event_1", True),
                    ("events", "expired_event_1", True),
                    ("planning", "active_plan_1", True),
                    ("planning", "expired_plan_1", True),
                    ("assignments", assignment_1_id, True),
                    ("assignments", assignment_2_id, False),
                ]
            )

    async def test_purge_all_locks(self):
        async with self.app.app_context():
            await purge_expired_locks_handler("all")
            await self.assertLockState(
                [
                    ("events", "active_event_1", True),
                    ("events", "expired_event_1", False),
                    ("planning", "active_plan_1", True),
                    ("planning", "expired_plan_1", False),
                    ("assignments", assignment_1_id, True),
                    ("assignments", assignment_2_id, False),
                ]
            )

    async def test_purge_all_locks_with_custom_expiry(self):
        async with self.app.app_context():
            await purge_expired_locks_handler("all", 2)
            await self.assertLockState(
                [
                    ("events", "active_event_1", False),
                    ("events", "expired_event_1", False),
                    ("planning", "active_plan_1", False),
                    ("planning", "expired_plan_1", False),
                    ("assignments", assignment_1_id, False),
                    ("assignments", assignment_2_id, False),
                ]
            )
