from __future__ import annotations

import uuid
import threading
from typing import Iterable
from ..core.models import ProviderCapabilities, ModelSelection
from .base import ProviderEvent, ProviderResult
from ..runtime.execution_dispatch import ExecutionNotStarted


class LocalCommandProvider:
    provider_id = "local-command"

    def __init__(self, command: list[str], *, timeout: float = 300.0,
                 execution_runner=None, execution_request_factory=None):
        if not command:
            raise ValueError("command_required")
        self.execution_runner = execution_runner
        self.execution_request_factory = execution_request_factory
        self._executor_sessions = {}
        self.command = list(command)
        self.timeout = float(timeout)
        self._lock = threading.RLock()
        self._cancelled = set()

    def capabilities(self) -> ProviderCapabilities:
        can_cancel = False
        if self.execution_runner is not None:
            can_cancel = not self.execution_runner.adapter.capabilities().missing()
        return ProviderCapabilities(can_create_child_session=True, can_stream_output=True, can_cancel_output=can_cancel)

    def models(self) -> list[str]:
        return ["local-command"]

    def select_model(self, requested_model):
        if requested_model and requested_model != "local-command":
            return ModelSelection(requested_model, None, "unsupported", "local command has no such model")
        return ModelSelection(requested_model, "local-command", "applied")

    def create_child_session(self, member_id, prompt, model=None):
        return ProviderResult("accepted", provider_event_id=f"local_{uuid.uuid4().hex}", model="local-command")

    def stream(self, provider_session_id: str, prompt: str) -> Iterable[ProviderEvent]:
        if self.execution_runner is not None:
            yield from self._stream_executor(provider_session_id, prompt)
            return
        yield ProviderEvent(f'{provider_session_id}:unavailable', 'status', status='failed', reason='execution_backend_required')
        return

    def _stream_executor(self, provider_session_id, prompt):
        if self.execution_request_factory is None:
            yield ProviderEvent(f'{provider_session_id}:unavailable', 'status', status='failed', reason='provider_execution_permit_unavailable')
            return
        with self._lock:
            if provider_session_id in self._cancelled:
                yield ProviderEvent(f'{provider_session_id}:cancelled', 'status', status='cancelled')
                return
            try:
                request, authorization_valid = self.execution_request_factory(provider_session_id, prompt, tuple(self.command), self.timeout)
            except (OSError, ValueError, RuntimeError) as exc:
                yield ProviderEvent(f'{provider_session_id}:permit-invalid', 'status', status='failed', reason='provider_execution_permit_invalid', metadata={'error_type': type(exc).__name__})
                return
            self._executor_sessions[provider_session_id] = request.execution_id
        try:
            def still_authorized():
                with self._lock:
                    cancelled = provider_session_id in self._cancelled
                return not cancelled and authorization_valid()
            observation = self.execution_runner.run(request, authorization_valid=still_authorized)
        except ExecutionNotStarted:
            with self._lock:
                self._executor_sessions.pop(provider_session_id, None)
            yield ProviderEvent(f'{provider_session_id}:cancelled', 'status', status='cancelled', reason='provider_execution_not_started')
            return
        except (OSError, ValueError, RuntimeError) as exc:
            yield ProviderEvent(f'{provider_session_id}:unconfirmed', 'status', status='unknown', reason='provider_executor_unconfirmed', metadata={'error_type': type(exc).__name__})
            return
        if observation.tree_exit_confirmed:
            with self._lock:
                self._executor_sessions.pop(provider_session_id, None)
        if observation.state == 'success':
            yield ProviderEvent(f'{provider_session_id}:output', 'output', text=observation.metadata.get('stdout', ''), model='local-command')
        status = 'completed' if observation.state == 'success' else observation.state
        reason = observation.reason
        if reason == 'nonzero_exit' and observation.exit_code is not None:
            reason = f'exit_code:{observation.exit_code}'
        yield ProviderEvent(f'{provider_session_id}:{status}', 'status', status=status, reason=reason, model='local-command')

    def cancel(self, provider_session_id):
        if self.execution_runner is not None:
            with self._lock:
                execution_id = self._executor_sessions.get(provider_session_id)
                self._cancelled.add(provider_session_id)
            if execution_id is None:
                return ProviderResult('cancelled', reason='provider_execution_not_started')
            observation = self.execution_runner.cancel(execution_id, reason='provider_cancelled')
            status = 'cancelled' if observation.state == 'cancelled' and observation.tree_exit_confirmed else 'unknown' if observation.state == 'unknown' else 'cancelling'
            return ProviderResult(status, reason=observation.reason)
        with self._lock:
            self._cancelled.add(provider_session_id)
        return ProviderResult('cancelled', reason='provider_execution_not_started')
