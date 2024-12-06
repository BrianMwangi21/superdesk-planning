# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2013, 2014, 2015, 2016, 2017, 2018 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

from datetime import timedelta, datetime
from bson.objectid import ObjectId
from contextvars import ContextVar

from planning.events import EventsAsyncService
from planning.planning import PlanningAsyncService
from superdesk.core import get_app_config
from superdesk.resource_fields import ID_FIELD
from superdesk import get_resource_service
from superdesk.logging import logger
from superdesk.utc import utcnow
from superdesk.celery_task_utils import get_lock_id
from superdesk.lock import lock, unlock, remove_locks
from superdesk.notification import push_notification
from .async_cli import planning_cli

from planning.utils import get_related_planning_for_events, get_related_event_ids_for_planning


log_msg_context: ContextVar[str] = ContextVar("log_msg", default="")


@planning_cli.command("planning:flag_expired")
async def flag_expired_items_command():
    """
    Flag expired `Events` and `Planning` items with `{'expired': True}`.

    Example:
    ::

        $ python manage.py planning:flag_expired

    """
    return await flag_expired_items_handler()


async def flag_expired_items_handler():
    now = utcnow()
    log_msg = f"Expiry Time: {now}."
    log_msg_context.set(log_msg)

    logger.info(f"{log_msg} Starting to remove expired content at.")

    expire_interval = get_app_config("PLANNING_EXPIRY_MINUTES", 0)
    if expire_interval == 0:
        logger.info(f"{log_msg} PLANNING_EXPIRY_MINUTES=0, not flagging items as expired")
        return

    lock_name = get_lock_id("planning", "flag_expired")
    if not lock(lock_name, expire=610):
        logger.info(f"{log_msg} Flag expired items task is already running")
        return

    expiry_datetime = now - timedelta(minutes=expire_interval)

    try:
        await flag_expired_events(expiry_datetime)
    except Exception as e:
        logger.exception(e)

    try:
        await flag_expired_planning(expiry_datetime)
    except Exception as e:
        logger.exception(e)

    unlock(lock_name)

    logger.info(f"{log_msg} Completed flagging expired items.")
    remove_locks()
    logger.info(f"{log_msg} Starting to remove expired planning versions.")
    remove_expired_published_planning()
    logger.info(f"{log_msg} Completed removing expired planning versions.")


async def flag_expired_events(expiry_datetime: datetime):
    log_msg = log_msg_context.get()
    logger.info(f"{log_msg} Starting to flag expired events")
    events_service = EventsAsyncService()
    planning_service = PlanningAsyncService()

    locked_events = set()
    events_in_use = set()
    events_expired = set()
    plans_expired = set()

    # Obtain the full list of Events that we're to process first
    # As subsequent queries will change the list of returned items
    events = dict()
    async for items in events_service.get_expired_items(expiry_datetime):
        events.update({item[ID_FIELD]: item for item in items})

    set_event_plans(events)

    for event_id, event in events.items():
        if event.get("lock_user"):
            locked_events.add(event_id)
        elif get_event_schedule(event) > expiry_datetime:
            events_in_use.add(event_id)
        else:
            events_expired.add(event_id)
            await events_service.system_update(event_id, {"expired": True})
            for plan in event.get("_plans", []):
                plan_id = plan[ID_FIELD]
                await planning_service.system_update(plan_id, {"expired": True})
                plans_expired.add(plan_id)

    if len(locked_events) > 0:
        logger.info(f"{log_msg} Skipping {len(locked_events)} locked Events: {list(locked_events)}")

    if len(events_in_use) > 0:
        logger.info(f"{log_msg} Skipping {len(events_in_use)} Events in use: {list(events_in_use)}")

    if len(events_expired) > 0:
        push_notification("events:expired", items=list(events_expired))

    if len(plans_expired) > 0:
        push_notification("planning:expired", items=list(plans_expired))

    logger.info(f"{log_msg} {len(events_expired)} Events expired: {list(events_expired)}")


async def flag_expired_planning(expiry_datetime: datetime):
    log_msg = log_msg_context.get()
    logger.info(f"{log_msg} Starting to flag expired planning items")
    planning_service = PlanningAsyncService()

    # Obtain the full list of Planning items that we're to process first
    # As subsequent queries will change the list of returnd items
    plans = dict()
    async for items in planning_service.get_expired_items(expiry_datetime):
        plans.update({item[ID_FIELD]: item for item in items})

    locked_plans = set()
    plans_expired = set()

    for plan_id, plan in plans.items():
        if plan.get("lock_user"):
            locked_plans.add(plan_id)
        else:
            await planning_service.system_update(plan[ID_FIELD], {"expired": True})
            plans_expired.add(plan_id)

    if len(locked_plans) > 0:
        logger.info(f"{log_msg} Skipping {len(locked_plans)} locked Planning items: {list(locked_plans)}")

    if len(plans_expired) > 0:
        push_notification("planning:expired", items=list(plans_expired))

    logger.info(f"{log_msg} {len(plans_expired)} Planning items expired: {list(plans_expired)}")


def set_event_plans(events):
    for plan in get_related_planning_for_events(list(events.keys()), "primary"):
        for related_event_id in get_related_event_ids_for_planning(plan, "primary"):
            event = events[related_event_id]
            if "_plans" not in event:
                event["_plans"] = []
            event["_plans"].append(plan)


def get_event_schedule(event):
    latest_scheduled = datetime.strptime(event["dates"]["end"], "%Y-%m-%dT%H:%M:%S%z")
    for plan in event.get("_plans", []):
        # First check the Planning item's planning date
        # and compare to the Event's end date
        if latest_scheduled < plan.get("planning_date", latest_scheduled):
            latest_scheduled = plan.get("planning_date")

        # Next go through all the coverage's scheduled dates
        # and compare to the latest scheduled date
        for planning_schedule in plan.get("_planning_schedule", []):
            scheduled = planning_schedule.get("scheduled")
            if scheduled and isinstance(scheduled, str):
                scheduled = datetime.strptime(planning_schedule.get("scheduled"), "%Y-%m-%dT%H:%M:%S%z")

            if scheduled and (latest_scheduled < scheduled):
                latest_scheduled = scheduled

    # Finally return the latest scheduled date among the Event, Planning and Coverages
    return latest_scheduled


def remove_expired_published_planning():
    """Expire planning versions

    Expiry of the planning versions mirrors the expiry of items within the publish queue in Superdesk so it uses the
    same configuration value

    :param self:
    :return:
    """
    expire_interval = get_app_config("PUBLISH_QUEUE_EXPIRY_MINUTES", 0)
    if expire_interval:
        expire_time = utcnow() - timedelta(minutes=expire_interval)
        logger.info("Removing planning history items created before {}".format(str(expire_time)))

        get_resource_service("published_planning").delete({"_id": {"$lte": ObjectId.from_datetime(expire_time)}})
