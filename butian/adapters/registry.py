from __future__ import annotations

from typing import Any
from .base import AgentProvider, UnsupportedProvider


class ProviderRegistry:
    """Runtime registry. Unknown providers remain explicit unsupported objects."""

    def __init__(self) -> None:
        self._providers: dict[str, AgentProvider] = {}

    def register(self, provider: AgentProvider) -> None:
        provider_id = str(provider.provider_id).strip()
        if not provider_id:
            raise ValueError("provider_id_required")
        self._providers[provider_id] = provider

    def get(self, provider_id: str | None) -> AgentProvider:
        if not provider_id:
            return UnsupportedProvider()
        return self._providers.get(provider_id, UnsupportedProvider())

    def capabilities(self, provider_id: str | None) -> dict[str, Any]:
        provider = self.get(provider_id)
        caps = provider.capabilities()
        return {name: getattr(caps, name) for name in caps.__dataclass_fields__}

    def list(self) -> list[str]:
        return sorted(self._providers)
