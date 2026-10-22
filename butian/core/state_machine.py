from __future__ import annotations
from .models import TaskStatus

_ALLOWED: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.TODO: {TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.READY: {TaskStatus.CLAIMED, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.CLAIMED: {TaskStatus.RUNNING, TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {TaskStatus.REVIEW, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLING, TaskStatus.WAITING_USER, TaskStatus.PROVIDER_LOST, TaskStatus.UNKNOWN},
    TaskStatus.REVIEW: {TaskStatus.DONE, TaskStatus.READY, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.WAITING_USER: {TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.UNKNOWN},
    TaskStatus.CANCELLING: {TaskStatus.CANCELLED, TaskStatus.PROVIDER_LOST, TaskStatus.UNKNOWN},
    TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.FAILED: {TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.PROVIDER_LOST: {TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.UNKNOWN},
    TaskStatus.UNKNOWN: {TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.DONE: set(),
    TaskStatus.CANCELLED: set(),
}

class InvalidTransition(ValueError):
    pass

def allowed_targets(current) -> list[str]:
    """当前状态可以合法迁移到的状态名；供错误提示给出可执行下一步。"""
    if isinstance(current, str):
        current = TaskStatus(current)
    return sorted(target.value for target in _ALLOWED.get(current, set()))


def transition(current: TaskStatus, target: TaskStatus) -> TaskStatus:
    if target not in _ALLOWED[current]:
        raise InvalidTransition(f"invalid task transition: {current} -> {target}")
    return target
