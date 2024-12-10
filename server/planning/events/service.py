from typing import AsyncGenerator, Any
from datetime import datetime
from superdesk.core.utils import date_to_str

from planning.types import EventResourceModel
from planning.common import get_max_recurrent_events, WORKFLOW_STATE
from planning.core.service import BasePlanningAsyncService


class EventsAsyncService(BasePlanningAsyncService[EventResourceModel]):
    resource_name = "events"

    async def get_expired_items(
        self, expiry_datetime: datetime, spiked_events_only: bool = False
    ) -> AsyncGenerator[list[dict[str, Any]], None]:
        """
        Retrieve "expired" events which are those whose end date is on or before `expiry_datetime` and
        are not already marked as expired.

        By default, items returned are:
        - Not expired.
        - Have an end date `<= expiry_datetime`.

        If `spiked_events_only` is True, only spiked events are returned, still filtered by
        end date `<= expiry_datetime`.

        Results are sorted by start date and fetched in batches.
        """
        query: dict[str, Any] = {
            "query": {
                "bool": {
                    "must_not": [{"term": {"expired": True}}],
                    "filter": {"range": {"dates.end": {"lte": date_to_str(expiry_datetime)}}},
                },
            },
            "sort": [{"dates.start": "asc"}],
            "size": get_max_recurrent_events(),
        }

        if spiked_events_only:
            del query["query"]["bool"]["must_not"]
            query["query"]["bool"]["must"] = [{"term": {"state": WORKFLOW_STATE.SPIKED}}]

        total_received = 0
        total_events = -1

        while True:
            query["from"] = total_received

            results = await self.search(query)
            items = await results.to_list_raw()
            results_count = len(items)

            # If the total_events has not been set, then this is the first query
            # In which case we need to store the total hits from the search
            if total_events < 0:
                total_events = results_count

                # If the search doesn't contain any results, return here
                if total_events < 1:
                    break

            # If the last query doesn't contain any results, return here
            if results_count == 0:
                break

            total_received += results_count

            # Yield the results for iteration by the callee
            yield items
