from __future__ import annotations

import uuid
from ..core.models import ProviderCapabilities, ModelSelection
from .base import ProviderEvent, ProviderResult


class ManualProvider:
    provider_id = "manual"

    def capabilities(self):
        return ProviderCapabilities(can_create_child_session=True, can_receive_user_reply=True)

    def models(self):
        return ["human"]

    def select_model(self, requested_model):
        return ModelSelection(requested_model, "human", "applied")

    def create_child_session(self, member_id, prompt, model=None):
        return ProviderResult("accepted", provider_event_id=f"manual_{uuid.uuid4().hex}", model="human", reason="awaiting_human_execution")

    def stream(self, provider_session_id, prompt):
        yield ProviderEvent(f"{provider_session_id}:waiting", "status", status="unknown", reason="awaiting_human_execution")

    def cancel(self, provider_session_id):
        return ProviderResult("cancelled", reason="manual_work_cancelled")
