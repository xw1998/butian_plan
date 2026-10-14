"""Host execution seam: native callbacks own processes, not the model session."""
from __future__ import annotations

import threading
from typing import Callable

from .execution_contract import ExecutionAdapter, ExecutionCapabilities, ExecutionObservation, ExecutionRequest


class HostExecutionAdapter:
    def __init__(self, *, capabilities: Callable[[], ExecutionCapabilities],
                 start: Callable[[ExecutionRequest], ExecutionObservation],
                 inspect: Callable[[str], ExecutionObservation],
                 events: Callable[[str, int], list[ExecutionObservation]],
                 cancel: Callable[[str, str], ExecutionObservation], close: Callable[[], None],
                 authorize: Callable[[str, int, float], object] | None = None,
                 renew: Callable[[str, float], object] | None = None,
                 revoke: Callable[[str, int], object] | None = None):
        self._capabilities, self._start, self._inspect = capabilities, start, inspect
        self._events, self._cancel, self._close = events, cancel, close
        self._authorize, self._renew, self._revoke = authorize, renew, revoke
        self._requests: dict[str, str] = {}
        self._observations: dict[str, ExecutionObservation] = {}
        self._lock = threading.RLock()

    def capabilities(self) -> ExecutionCapabilities:
        result = self._capabilities()
        if not isinstance(result, ExecutionCapabilities):
            raise ValueError('host_execution_capabilities_invalid')
        return result

    def _identity_matches(self, execution_id, value, capabilities):
        if not isinstance(value, ExecutionObservation) or value.execution_id != execution_id or value.executor_id != capabilities.executor_id:
            return False
        if value.instance_id == capabilities.instance_id:
            return True
        previous = self._observations.get(execution_id)
        return bool(previous is not None
            and previous.executor_id == value.executor_id
            and previous.instance_id == value.instance_id
            and value.state == 'unknown' and value.reason == 'executor_instance_lost'
            and not value.tree_exit_confirmed
            and value.metadata.get('recovery_instance_id') == capabilities.instance_id)

    def _validate(self, execution_id: str, value: ExecutionObservation,
                  capabilities: ExecutionCapabilities) -> ExecutionObservation:
        if not self._identity_matches(execution_id, value, capabilities):
            raise ValueError('host_execution_identity_mismatch')
        previous = self._observations.get(execution_id)
        if previous is not None:
            if value.sequence < previous.sequence or (value.sequence == previous.sequence and value != previous):
                raise ValueError('host_execution_event_conflict')
            if ((previous.state == 'running' and value.state == 'accepted')
                    or (previous.state == 'cancelling' and value.state in {'accepted', 'running'})):
                raise ValueError('host_execution_state_regression')
            exit_confirmation = (previous.state == value.state == 'unknown'
                and value.sequence > previous.sequence
                and not previous.tree_exit_confirmed and value.tree_exit_confirmed
                and previous.reason == value.reason)
            if previous.state in {'success', 'failed', 'cancelled', 'unknown'} and value != previous and not exit_confirmation:
                raise ValueError('host_execution_terminal_conflict')
        self._observations[execution_id] = value
        return value

    def start(self, request: ExecutionRequest) -> ExecutionObservation:
        with self._lock:
            capabilities = self.capabilities()
            capabilities.require()
            previous = self._requests.get(request.execution_id)
            if previous is not None:
                if previous != request.request_sha256:
                    raise ValueError('execution_id_request_conflict')
                return self._validate(request.execution_id, self._inspect(request.execution_id), capabilities)
            # Record before calling the host. A lost response must be reconciled by
            # inspect; retrying start could duplicate an externally visible action.
            self._requests[request.execution_id] = request.request_sha256
            return self._validate(request.execution_id, self._start(request), capabilities)

    def inspect(self, execution_id: str) -> ExecutionObservation:
        with self._lock:
            return self._validate(execution_id, self._inspect(execution_id), self.capabilities())

    def events(self, execution_id: str, after_sequence: int = 0) -> list[ExecutionObservation]:
        with self._lock:
            capabilities = self.capabilities()
            values = self._events(execution_id, after_sequence)
            expected = after_sequence + 1
            previous = self._observations.get(execution_id)
            try:
                for value in values:
                    if not isinstance(value, ExecutionObservation) or value.sequence != expected:
                        raise ValueError('host_execution_event_sequence_gap')
                    if not self._identity_matches(execution_id, value, capabilities):
                        raise ValueError('host_execution_identity_mismatch')
                    # Validate historical transitions too, without treating an
                    # earlier replayed sequence as a live state regression.
                    if expected == after_sequence + 1 and previous is not None and value.sequence < previous.sequence:
                        self._observations.pop(execution_id, None)
                    if previous is not None and value.sequence == previous.sequence and value != previous:
                        raise ValueError('host_execution_event_conflict')
                    self._validate(execution_id, value, capabilities)
                    expected += 1
                current = self._observations.get(execution_id)
                if previous is not None and (current is None or current.sequence < previous.sequence):
                    self._observations[execution_id] = previous
                elif previous is not None and current.sequence == previous.sequence and current != previous:
                    raise ValueError('host_execution_event_conflict')
            except BaseException:
                if previous is None:
                    self._observations.pop(execution_id, None)
                else:
                    self._observations[execution_id] = previous
                raise
            return values

    def cancel(self, execution_id: str, *, reason: str) -> ExecutionObservation:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('execution_cancel_reason_required')
        with self._lock:
            capabilities = self.capabilities()
            capabilities.require(('force_stop', 'process_tree_cleanup', 'exit_confirmation'))
            return self._validate(execution_id, self._cancel(execution_id, reason), capabilities)

    def authorize(self, authorization_id: str, generation: int, expires_at: float):
        if self._authorize is None or self._renew is None or self._revoke is None:
            raise ValueError('host_execution_authorization_control_unavailable')
        return self._authorize(authorization_id, generation, expires_at)

    def renew(self, execution_id: str, lease_seconds: float):
        if self._renew is None:
            raise ValueError('host_execution_authorization_control_unavailable')
        return self._renew(execution_id, lease_seconds)

    def revoke(self, authorization_id: str, generation: int):
        if self._revoke is None:
            raise ValueError('host_execution_authorization_control_unavailable')
        return self._revoke(authorization_id, generation)

    def close(self) -> None:
        self._close()
