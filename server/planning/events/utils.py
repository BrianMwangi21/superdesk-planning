from typing import AsyncGenerator, Any, Tuple
from datetime import datetime

from planning.common import (
    WORKFLOW_STATE,
    get_max_recurrent_events,
)
from planning.types import EventResourceModel
from superdesk.core.types import SortParam, SortListParam
from superdesk.resource_fields import ID_FIELD
from superdesk.utc import utcnow


async def get_series(
    query: dict, sort: SortParam | None = None, max_results: int = 25
) -> AsyncGenerator[dict[str, Any]]:
    events_service = EventResourceModel.get_service()
    page = 1

    while True:
        # Get the results from mongo
        results = await events_service.find(req=query, page=page, max_results=max_results, sort=sort, use_mongo=True)

        docs = await results.to_list_raw()
        if not docs:
            break

        page += 1

        # Yield the results for iteration by the callee
        for doc in docs:
            yield doc


async def get_recurring_timeline(
    selected: dict[str, Any],
    spiked: bool = False,
    rescheduled: bool = False,
    cancelled: bool = False,
    postponed: bool = False,
) -> Tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Utility method to get all events in the series

    This splits up the series of events into 3 separate arrays.
    Historic: event.dates.start < utcnow()
    Past: utcnow() < event.dates.start < selected.dates.start
    Future: event.dates.start > selected.dates.start
    """
    excluded_states = []

    if not spiked:
        excluded_states.append(WORKFLOW_STATE.SPIKED)
    if not rescheduled:
        excluded_states.append(WORKFLOW_STATE.RESCHEDULED)
    if not cancelled:
        excluded_states.append(WORKFLOW_STATE.CANCELLED)
    if not postponed:
        excluded_states.append(WORKFLOW_STATE.POSTPONED)

    query = {
        "$and": [
            {"recurrence_id": selected["recurrence_id"]},
            {"_id": {"$ne": selected[ID_FIELD]}},
        ]
    }

    if excluded_states:
        query["$and"].append({"state": {"$nin": excluded_states}})

    sort: SortListParam = [("dates.start", 1)]
    max_results = get_max_recurrent_events()
    selected_start = selected.get("dates", {}).get("start", utcnow())

    # Make sure we are working with a datetime instance
    if not isinstance(selected_start, datetime):
        selected_start = datetime.strptime(selected_start, "%Y-%m-%dT%H:%M:%S%z")

    historic = []
    past = []
    future = []

    async for event in get_series(query, sort, max_results):
        event["dates"]["end"] = event["dates"]["end"]
        event["dates"]["start"] = event["dates"]["start"]
        for sched in event.get("_planning_schedule", []):
            sched["scheduled"] = sched["scheduled"]
        end = event["dates"]["end"]
        start = event["dates"]["start"]
        if end < utcnow():
            historic.append(event)
        elif start < selected_start:
            past.append(event)
        elif start > selected_start:
            future.append(event)

    return historic, past, future
