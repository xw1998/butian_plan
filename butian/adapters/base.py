from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol
from ..core.models import ProviderCapabilities, ModelSelection

@dataclass(frozen=True)
class ProviderResult:
    status: str
    text: str = ""
    provider_event_id: str | None = None
    model: str | None = None
    reason: str | None = None

class AgentProvider(Protocol):
    """Capability-negotiated boundary for any external agent runtime."""
    provider_id: str
    def capabilities(self) -> ProviderCapabilities: ...
    def select_model(self, requested_model: str | None) -> ModelSelection: ...
    def create_child_session(self, member_id: str, prompt: str, model: str | None = None) -> ProviderResult: ...
    def cancel(self, provider_session_id: str) -> ProviderResult: ...

class UnsupportedProvider:
    provider_id = "unsupported"
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()
    def select_model(self, requested_model):
        return ModelSelection(requested_model, None, "unsupported", "provider is not installed")
    def create_child_session(self, member_id, prompt, model=None):
        return ProviderResult("unsupported", reason="provider is not installed")
    def cancel(self, provider_session_id):
        return ProviderResult("unsupported", reason="provider is not installed")
