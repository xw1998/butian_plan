from __future__ import annotations

from typing import Any
from ..adapters.base import AgentProvider
from ..adapters.registry import ProviderRegistry
from .store import Store

class ProviderRuntime:
    def __init__(self, store: Store, registry: ProviderRegistry | None = None):
        self.store = store
        self.registry = registry or ProviderRegistry()

    def start(self, member_id: str, prompt: str, *, task_id: str | None = None, provider_id: str | None = None, model: str | None = None) -> dict[str, Any]:
        provider = self.registry.get(provider_id)
        selection = provider.select_model(model)
        if selection.selection_status in {"unsupported", "unknown"}:
            return {"status": selection.selection_status, "provider_id": provider.provider_id, "reason": selection.fallback_reason}
        created = provider.create_child_session(member_id, prompt, selection.effective_model)
        if created.status not in {"accepted", "running", "completed"}:
            return {"status": created.status, "provider_id": provider.provider_id, "reason": created.reason}
        provider_session_id = self.store.create_provider_session(member_id, provider.provider_id, task_id=task_id, model=selection.effective_model, remote_session_id=created.provider_event_id, prompt=prompt)
        return {"status": "running", "provider_session_id": provider_session_id, "provider_id": provider.provider_id, "model": selection.effective_model}

    def consume(self, provider_session_id: str, provider: AgentProvider, prompt: str | None = None) -> dict[str, Any]:
        if prompt is None:
            prompt = self.store.provider_session(provider_session_id).get("prompt")
            if not prompt:
                raise ValueError("provider_session_prompt_missing")
        output = ""
        final_status: str | None = None
        reason: str | None = None
        session = self.store.provider_session(provider_session_id)
        remote_session_id = session.get("remote_session_id") or provider_session_id
        try:
            for event in provider.stream(remote_session_id, prompt):
                if event.kind == "output":
                    if self.store.record_provider_output(provider_session_id, event.event_id, event.text, event.model):
                        output += event.text
                elif event.kind == "status":
                    final_status, reason = self._map_status(event.status, event.reason)
        except Exception as exc:
            final_status, reason = "unknown", f"provider_stream_error:{type(exc).__name__}"
        if final_status is None:
            final_status, reason = "unknown", "provider_stream_ended_without_terminal_status"
        return self.store.finish_provider_session(provider_session_id, final_status, reason) | {"text": output}

    @staticmethod
    def _map_status(status: str | None, reason: str | None) -> tuple[str, str | None]:
        if status in {"completed", "cancelled", "provider_lost", "unknown", "cancelling"}:
            return status, reason
        if status == "failed":
            return "unknown", reason or "provider_failed_without_recoverability"
        return "unknown", reason or "provider_returned_unknown_status"
