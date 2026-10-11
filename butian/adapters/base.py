from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol
from ..core.models import ProviderCapabilities, ModelSelection


@dataclass(frozen=True)
class ProviderEvent:
    event_id: str
    kind: str
    text: str = ""
    model: str | None = None
    status: str | None = None
    reason: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


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
    def models(self) -> list[str]: ...
    def select_model(self, requested_model: str | None) -> ModelSelection: ...
    def create_child_session(self, member_id: str, prompt: str, model: str | None = None) -> ProviderResult: ...
    def stream(self, provider_session_id: str, prompt: str) -> Iterable[ProviderEvent]: ...
    def cancel(self, provider_session_id: str) -> ProviderResult: ...


class UnsupportedProvider:
    provider_id = "unsupported"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def models(self) -> list[str]:
        return []

    def select_model(self, requested_model):
        return ModelSelection(requested_model, None, "unsupported", "provider is not installed")

    def create_child_session(self, member_id, prompt, model=None):
        return ProviderResult("unsupported", reason="provider is not installed")

    def stream(self, provider_session_id, prompt):
        yield ProviderEvent("unsupported", "status", status="unsupported", reason="provider is not installed")

    def cancel(self, provider_session_id):
        return ProviderResult("unsupported", reason="provider is not installed")
