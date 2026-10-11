from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .registry import ProviderRegistry
from .http import OpenAICompatibleProvider, OllamaProvider
from .local import LocalCommandProvider
from .manual import ManualProvider
from .mcp import McpProvider

CONFIG_FILENAME = "providers.json"

KNOWN_TYPES = {
    "openai-compatible",
    "ollama",
    "local-command",
    "manual",
    "mcp",
}


@dataclass(frozen=True)
class ProviderConfigError:
    index: int
    provider_id: str
    reason: str


def _resolve_api_key(entry: dict[str, Any]) -> str | None:
    if "api_key" in entry and entry["api_key"]:
        return str(entry["api_key"])
    env_name = entry.get("api_key_env")
    if env_name:
        value = os.environ.get(str(env_name))
        if not value:
            # 明确报缺失，而不是静默发出必然 401 的请求
            raise ValueError(f"api_key_env_not_set:{env_name}")
        return value
    return None


def _build_provider(entry: dict[str, Any]) -> Any:
    ptype = str(entry.get("type", "")).strip()
    if ptype not in KNOWN_TYPES:
        raise ValueError(f"unknown_provider_type:{ptype or 'missing'}")

    if ptype == "openai-compatible":
        base_url = entry.get("base_url")
        if not base_url:
            raise ValueError("base_url_required")
        return OpenAICompatibleProvider(
            str(base_url),
            api_key=_resolve_api_key(entry),
            provider_id=str(entry.get("provider_id") or "openai-compatible"),
            model=str(entry.get("model") or "unknown"),
        )
    if ptype == "ollama":
        base_url = entry.get("base_url") or "http://127.0.0.1:11434/v1"
        return OllamaProvider(str(base_url), model=str(entry.get("model") or "unknown"))
    if ptype == "local-command":
        command = entry.get("command")
        if not isinstance(command, list) or not command:
            raise ValueError("command_required")
        return LocalCommandProvider([str(item) for item in command], timeout=float(entry.get("timeout", 300.0)))
    if ptype == "manual":
        return ManualProvider()
    # mcp
    command = entry.get("command")
    if not isinstance(command, list) or not command:
        raise ValueError("command_required")
    return McpProvider([str(item) for item in command], timeout=float(entry.get("timeout", 30.0)))


def load_provider_config(path: str | Path | None) -> list[dict[str, Any]]:
    """Read and structurally validate a provider config JSON file."""
    if path is None:
        return []
    file_path = Path(path)
    if not file_path.exists():
        return []
    with file_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("provider_config_must_be_object")
    providers = data.get("providers", [])
    if not isinstance(providers, list):
        raise ValueError("providers_must_be_list")
    for index, entry in enumerate(providers):
        if not isinstance(entry, dict):
            raise ValueError(f"provider_entry_{index}_must_be_object")
    return providers


def config_path_for_home(home: Path) -> Path:
    return home / CONFIG_FILENAME


def build_registry(config: list[dict[str, Any]] | None) -> tuple[ProviderRegistry, list[ProviderConfigError]]:
    """Build a registry from config entries. Invalid entries are reported, never silently skipped.

    Manual provider is always registered so a human can execute tasks even with empty config.
    """
    registry = ProviderRegistry()
    errors: list[ProviderConfigError] = []
    for index, entry in enumerate(config or []):
        try:
            provider = _build_provider(entry)
            registry.register(provider)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(ProviderConfigError(index, str(entry.get("provider_id") or entry.get("type") or f"entry_{index}"), str(exc)))
    registry.register(ManualProvider())
    return registry, errors


def load_registry_from_env(home: Path) -> tuple[ProviderRegistry, list[ProviderConfigError], str | None]:
    """Resolve config location: BUTIAN_PROVIDERS_FILE env var first, then <home>/providers.json."""
    env_path = os.environ.get("BUTIAN_PROVIDERS_FILE")
    path: str | None = None
    if env_path:
        path = env_path
        if not Path(env_path).exists():
            raise ValueError(f"butian_providers_file_not_found:{env_path}")
    else:
        default = config_path_for_home(home)
        if default.exists():
            path = str(default)
    config = load_provider_config(path)
    registry, errors = build_registry(config)
    return registry, errors, path
