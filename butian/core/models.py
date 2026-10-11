from __future__ import annotations
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

class Mode(StrEnum):
    SOLO = "solo"
    TEAM = "team"

class TaskStatus(StrEnum):
    TODO = "todo"
    READY = "ready"
    CLAIMED = "claimed"
    RUNNING = "running"
    REVIEW = "review"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_USER = "waiting_user"
    CANCELLING = "cancelling"
    PROVIDER_LOST = "provider_lost"
    UNKNOWN = "unknown"

@dataclass(frozen=True)
class Member:
    member_id: str
    name: str
    role: str = "member"
    responsibilities: tuple[str, ...] = ()
    provider_id: str | None = None
    model_id: str | None = None
    default_strategy_id: str = "experience_reuse"
    strategy_version: int = 1
    strategy_stages: tuple[str, ...] = ()

@dataclass
class Handoff:
    task_id: str
    task_revision: int
    member_id: str
    status: str
    summary: str
    work_done: list[str] = field(default_factory=list)
    work_not_done: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    questions_for_user: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict)
    next_action: str | None = None
    confidence: float = 0.0

@dataclass(frozen=True)
class ProviderCapabilities:
    can_create_child_session: bool = False
    can_stream_output: bool = False
    can_cancel_output: bool = False
    can_receive_user_reply: bool = False
    can_open_subagent_conversation: bool = False
    context_window: int | None = 8192

@dataclass(frozen=True)
class ModelSelection:
    requested_model: str | None
    effective_model: str | None
    selection_status: str
    fallback_reason: str | None = None
