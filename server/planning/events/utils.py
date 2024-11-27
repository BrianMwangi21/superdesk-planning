from datetime import datetime
from eve.utils import ParsedRequest
import json

from planning.common import (
    WORKFLOW_STATE,
    get_max_recurrent_events,
)
from planning.events import EventsAsyncService
from superdesk.resource_fields import ID_FIELD
from superdesk.utc import utcnow


async def get_series(query, sort, max_results):
    events_service = EventsAsyncService()
    page = 1

    while True:
        # Get the results from mongo
        req = ParsedRequest()
        req.sort = sort
        req.where = json.dumps(query)
        req.max_results = max_results
        req.page = page
        results = await events_service.get_from_mongo(req=req, lookup=None)

        docs = list(results)
        if not docs:
            break

        page += 1

        # Yield the results for iteration by the callee
        for doc in docs:
            yield doc


async def get_recurring_timeline(
    selected,
    spiked=False,
    rescheduled=False,
    cancelled=False,
    postponed=False,
):
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

    sort = '[("dates.start", 1)]'
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
