"""Execution lifecycle contract shared by local and host-owned adapters.

Implementation availability alone is not evidence of operating-system permission.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol

CapabilityState = Literal["supported", "unsupported", "unknown"]
TERMINAL_STATES = frozenset({"success", "failed", "cancelled", "unknown"})
REQUIRED_CONTROL = ("start", "force_stop", "process_tree_cleanup", "exit_confirmation")


@dataclass(frozen=True)
class CapabilityEvidence:
    state: CapabilityState = "unknown"
    source: str = "not_verified"
    checked_at: float | None = None
    instance_id: str = ""

    def __post_init__(self) -> None:
        if self.state not in {"supported", "unsupported", "unknown"}:
            raise ValueError("execution_capability_state_invalid")
        if self.checked_at is not None and (not math.isfinite(self.checked_at) or self.checked_at < 0):
            raise ValueError("execution_capability_time_invalid")

    def verified_for(self, instance_id: str) -> bool:
        return (self.state == "supported" and self.checked_at is not None
                and self.instance_id == instance_id and bool(instance_id)
                and self.source not in {"", "not_verified", "implementation"})


@dataclass(frozen=True)
class ExecutionCapabilities:
    executor_id: str
    instance_id: str
    features: Mapping[str, CapabilityEvidence] = field(default_factory=dict)

    def missing(self, required: tuple[str, ...] = REQUIRED_CONTROL) -> list[str]:
        return [name for name in required if not self.features.get(name, CapabilityEvidence()).verified_for(self.instance_id)]

    def require(self, required: tuple[str, ...] = REQUIRED_CONTROL) -> None:
        missing = self.missing(required)
        if missing:
            raise ValueError("execution_capabilities_missing:" + ",".join(missing))


@dataclass(frozen=True)
class ExecutionRequest:
    execution_id: str
    request_sha256: str
    argv: tuple[str, ...]
    cwd: str
    stdin: bytes
    timeout: float
    output_limit: int
    permit: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.execution_id or not self.argv or any(not isinstance(x, str) or not x or "\x00" in x for x in self.argv):
            raise ValueError("execution_request_invalid")
        if len(self.request_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.request_sha256):
            raise ValueError("execution_request_digest_invalid")
        if isinstance(self.timeout, bool) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("execution_timeout_invalid")
        if not isinstance(self.stdin, bytes) or len(self.stdin) > 1024 * 1024:
            raise ValueError('execution_input_limit_invalid')
        if isinstance(self.output_limit, bool) or not isinstance(self.output_limit, int) or not 0 < self.output_limit <= 4 * 1024 * 1024:
            raise ValueError("execution_output_limit_invalid")


@dataclass(frozen=True)
class ExecutionObservation:
    execution_id: str
    executor_id: str
    instance_id: str
    sequence: int
    state: str
    reason: str | None = None
    exit_code: int | None = None
    tree_exit_confirmed: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in {"accepted", "running", "cancelling", *TERMINAL_STATES}:
            raise ValueError("execution_state_invalid")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("execution_sequence_invalid")
        if self.state in {"success", "failed", "cancelled"} and not self.tree_exit_confirmed:
            raise ValueError("execution_exit_unconfirmed")
        if self.state == "success" and self.exit_code != 0:
            raise ValueError("execution_success_exit_invalid")


class ExecutionAdapter(Protocol):
    def capabilities(self) -> ExecutionCapabilities: ...
    def start(self, request: ExecutionRequest) -> ExecutionObservation: ...
    def inspect(self, execution_id: str) -> ExecutionObservation: ...
    def events(self, execution_id: str, after_sequence: int = 0) -> list[ExecutionObservation]: ...
    def cancel(self, execution_id: str, *, reason: str) -> ExecutionObservation: ...
    def authorize(self, authorization_id: str, generation: int, expires_at: float) -> Any: ...
    def renew(self, execution_id: str, lease_seconds: float) -> Any: ...
    def revoke(self, authorization_id: str, generation: int) -> Any: ...
    def close(self) -> None: ...


def required_execution_features(required):
    """Map business process-tree requirements to verified lifecycle evidence."""
    features = set(required) - {'process_tree_control'}
    if 'process_tree_control' in required:
        features.update(REQUIRED_CONTROL)
    return tuple(sorted(features))
