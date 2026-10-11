from __future__ import annotations

import subprocess
import uuid
from typing import Iterable
from ..core.models import ProviderCapabilities, ModelSelection
from .base import ProviderEvent, ProviderResult


class LocalCommandProvider:
    provider_id = "local-command"

    def __init__(self, command: list[str], *, timeout: float = 300.0):
        if not command:
            raise ValueError("command_required")
        self.command = list(command)
        self.timeout = float(timeout)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(can_create_child_session=True, can_stream_output=True, can_cancel_output=False)

    def models(self) -> list[str]:
        return ["local-command"]

    def select_model(self, requested_model):
        if requested_model and requested_model != "local-command":
            return ModelSelection(requested_model, None, "unsupported", "local command has no such model")
        return ModelSelection(requested_model, "local-command", "applied")

    def create_child_session(self, member_id, prompt, model=None):
        return ProviderResult("accepted", provider_event_id=f"local_{uuid.uuid4().hex}", model="local-command")

    def stream(self, provider_session_id: str, prompt: str) -> Iterable[ProviderEvent]:
        try:
            completed = subprocess.run(self.command, input=prompt, text=True, capture_output=True, timeout=self.timeout, check=False, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            yield ProviderEvent(f"{provider_session_id}:timeout", "status", status="unknown", reason="provider_timeout")
            return
        if completed.returncode != 0:
            yield ProviderEvent(f"{provider_session_id}:failed", "status", status="failed", reason=f"exit_code:{completed.returncode}", metadata={"stderr": completed.stderr[-4000:]})
            return
        yield ProviderEvent(f"{provider_session_id}:output", "output", text=completed.stdout, model="local-command")
        yield ProviderEvent(f"{provider_session_id}:completed", "status", status="completed", model="local-command")

    def cancel(self, provider_session_id):
        return ProviderResult("unsupported", reason="local command cancellation is not supported")
