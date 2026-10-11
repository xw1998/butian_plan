from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from .memory import MemoryStore
from .store import Store

# 分层字符预算：优先级 L0 > L1 > L2 > L3 > L4，窗口不足只裁剪低层
DEFAULT_BUDGETS = {
    "L1": 2000,
    "L2": 2000,
    "L3": 3000,
    "L4": 2000,
    "TOOLS": 2400,
}


@dataclass
class ContextReport:
    member_id: str
    task_id: str | None
    decision_id: str
    layers: dict[str, int] = field(default_factory=dict)  # layer -> 送入字符数
    trimmed: list[str] = field(default_factory=list)  # 被裁剪的层
    recalled_memory_ids: list[str] = field(default_factory=list)
    no_match: bool = True
    tools_included: int = 0
    tools_omitted: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "task_id": self.task_id,
            "decision_id": self.decision_id,
            "layers": self.layers,
            "trimmed": self.trimmed,
            "recalled_memory_ids": self.recalled_memory_ids,
            "no_match": self.no_match,
            "tools_included": self.tools_included,
            "tools_omitted": self.tools_omitted,
        }


class ContextComposer:
    """把人格、任务状态、近期事件、相关记忆按 L0–L4 组装成一次 Provider 调用的上下文。

    L0 当前指令（用户消息，永不裁剪）
    L1 当前任务状态包
    L2 近期事件
    L3 相关记忆（独立记忆库召回，跳过 invalidated/archived）
    L4 人格与职责（成员档案）
    """

    def __init__(self, store: Store, tool_store=None):
        self.store = store
        self.tool_store = tool_store

    def member_persona(self, member_id: str) -> dict[str, Any]:
        row = self.store.db.execute("SELECT member_id, name, role, responsibilities, persona FROM members WHERE member_id=?", (member_id,)).fetchone()
        if not row:
            raise ValueError("member_not_found")
        responsibilities = json.loads(row["responsibilities"] or "[]")
        return {
            "name": row["name"],
            "role": row["role"],
            "responsibilities": responsibilities,
            "persona": row["persona"],
        }

    def task_package(self, task_id: str) -> dict[str, Any] | None:
        row = self.store.db.execute("SELECT task_id, parent_id, session_id, title, status, assignee_id, revision, payload FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return None
        return {
            "task_id": row["task_id"],
            "parent_id": row["parent_id"],
            "session_id": row["session_id"],
            "title": row["title"],
            "status": row["status"],
            "assignee_id": row["assignee_id"],
            "revision": row["revision"],
            "payload": json.loads(row["payload"] or "{}"),
        }

    def recent_events(self, aggregate_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.store.db.execute(
            "SELECT event_type, payload, created_at FROM events WHERE aggregate_id=? ORDER BY seq DESC LIMIT ?",
            (aggregate_id, limit),
        ).fetchall()
        return [{"event_type": row["event_type"], "payload": json.loads(row["payload"]), "created_at": row["created_at"]} for row in rows]

    def compose(self, member_id: str, instruction: str, *, task_id: str | None = None, memory: MemoryStore | None = None, environment: dict[str, Any] | None = None, budgets: dict[str, int] | None = None, include_tools: bool = False) -> tuple[str, ContextReport]:
        if not instruction.strip():
            raise ValueError("instruction_required")
        budget = dict(DEFAULT_BUDGETS)
        budget.update(budgets or {})
        decision_id = f"dec_{uuid.uuid4().hex}"
        report = ContextReport(member_id=member_id, task_id=task_id, decision_id=decision_id)

        persona = self.member_persona(member_id)
        task = self.task_package(task_id) if task_id else None
        events = self.recent_events(task_id, limit=5) if task_id else []

        # L3 相关记忆召回
        memories: list[dict[str, Any]] = []
        if memory is not None:
            query_parts = [instruction.strip()]
            if task:
                query_parts.append(task.get("title") or "")
            query = " ".join(part for part in query_parts if part)[:200]
            memories = memory.recall_for_decision(query, limit=8, environment=environment, decision_id=decision_id)
        report.recalled_memory_ids = [item["memory_id"] for item in memories]
        report.no_match = not memories

        sections: list[str] = []
        # L0 —— 永不裁剪
        sections.append("【L0 当前指令】\n" + instruction.strip())
        report.layers["L0"] = len(sections[-1])

        # L1 当前任务状态包
        if task:
            text = "【L1 当前任务】\n" + json.dumps(task, ensure_ascii=False, indent=None)
            if len(text) > budget["L1"]:
                report.trimmed.append("L1")
            sections.append(text)
            report.layers["L1"] = len(text)

        # L2 近期事件
        if events:
            text = "【L2 近期事件】\n" + json.dumps(events, ensure_ascii=False)
            if len(text) > budget["L2"]:
                report.trimmed.append("L2")
            # L2 may be large, but keep complete event JSON so recovery context remains parseable.
            sections.append(text)
            report.layers["L2"] = len(text)

        # L3 相关记忆（标注状态，candidate/disputed 必须显式标记为参考）
        if memories:
            lines = ["【L3 相关记忆】（按权重召回；标注为参考的内容不是确定性指令）"]
            for item in memories:
                flag = "" if item["status"] in {"hot", "warm", "verified"} else f"[{item['status']}·仅供参考]"
                lines.append(f"- {flag}{item['content']}")
            text = "\n".join(lines)
            if len(text) > budget["L3"]:
                report.trimmed.append("L3")
                text = text[: budget["L3"]]
            sections.append(text)
            report.layers["L3"] = len(text)

        # 工具卡片独立预算：只放契约元数据，不放源码；预算不足时整卡省略。
        if include_tools and self.tool_store is not None:
            tool_budget = budget["TOOLS"]
            used = 0
            cards = self.tool_store.active_cards(member_id)
            for card in cards:
                card_text = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
                rendered = "- " + card_text
                if used + len(rendered) > tool_budget:
                    report.tools_omitted += 1
                    continue
                if report.tools_included == 0:
                    sections.append("【工具卡片】（仅含调用契约；源码不在上下文中）")
                    used += len(sections[-1])
                sections.append(rendered)
                used += len(rendered)
                report.tools_included += 1
            if cards and report.tools_omitted:
                report.trimmed.append("TOOLS")

        # L4 人格与职责
        persona_view = {key: value for key, value in persona.items() if value}
        text = "【L4 身份与职责】\n" + json.dumps(persona_view, ensure_ascii=False)
        if len(text) > budget["L4"]:
            report.trimmed.append("L4")
            text = text[: budget["L4"]]
        sections.append(text)
        report.layers["L4"] = len(text)

        return "\n\n".join(sections), report
