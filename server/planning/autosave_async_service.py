from planning.events.events_service import EventsAsyncService
from planning.types import EventResourceModel, PlanningResourceModel
from superdesk.core.resources import AsyncResourceService
from superdesk.errors import SuperdeskApiError
from apps.item_lock.components.item_lock import LOCK_SESSION, LOCK_USER


class AutosaveAsyncService(AsyncResourceService):
    """Async Service class for the Autosave model."""

    async def on_create(self, docs: list[EventResourceModel | PlanningResourceModel]) -> None:
        await super().on_create(docs)

        for doc in docs:
            self._validate(doc)
            delattr(doc, "expired")

    async def on_delete(self, doc: EventResourceModel | PlanningResourceModel):
        await super().on_delete(doc)

        if doc.type == "event":
            events_service = EventsAsyncService()
            await events_service.delete_event_files({}, doc.files)

    @staticmethod
    def _validate(doc: EventResourceModel | PlanningResourceModel):
        """Validate the autosave to ensure it contains user/session"""

        if not doc.lock_user:
            raise SuperdeskApiError.badRequestError(message="Autosave failed, User not supplied")

        if not doc.lock_session:
            raise SuperdeskApiError.badRequestError(message="Autosave failed, User Session not supplied")

    async def on_session_end(self, user_id, session_id, is_last_session):
        await self.delete_many(lookup={LOCK_USER: str(user_id)} if is_last_session else {LOCK_SESSION: str(session_id)})
