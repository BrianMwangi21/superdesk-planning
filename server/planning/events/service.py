from eve.utils import date_to_str

from planning.types import EventResourceModel
from planning.common import get_max_recurrent_events, WORKFLOW_STATE
from planning.core.service import BasePlanningAsyncService


class EventsAsyncService(BasePlanningAsyncService[EventResourceModel]):
    resource_name = "events"

    async def get_expired_items(self, expiry_datetime, spiked_events_only=False):
        """Get the expired items

        Where end date is in the past
        """
        query = {
            "query": {"bool": {"must_not": [{"term": {"expired": True}}]}},
            "filter": {"range": {"dates.end": {"lte": date_to_str(expiry_datetime)}}},
            "sort": [{"dates.start": "asc"}],
            "size": get_max_recurrent_events(),
        }

        if spiked_events_only:
            query["query"] = {"bool": {"must": [{"term": {"state": WORKFLOW_STATE.SPIKED}}]}}

        total_received = 0
        total_events = -1

        while True:
            query["from"] = total_received

            results = self.search(query)

            # If the total_events has not been set, then this is the first query
            # In which case we need to store the total hits from the search
            if total_events < 0:
                total_events = results.count()

                # If the search doesn't contain any results, return here
                if total_events < 1:
                    break

            # If the last query doesn't contain any results, return here
            if not len(results.docs):
                break

            total_received += len(results.docs)

            # Yield the results for iteration by the callee
            yield list(results.docs)
