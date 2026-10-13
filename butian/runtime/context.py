from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from .memory import MemoryStore
from .store import Store

DEFAULT_BUDGETS = {
    "L1": 2000,
    "L2": 2000,
    "L3": 3000,
    "L4": 2000,
    "TOOLS": 2400,
}
_TOOL_HEADER = "【工具卡片】（仅含调用契约；源码不在上下文中）"


@dataclass
class ContextReport:
    member_id: str
    task_id: str | None
    decision_id: str
    layers: dict[str, int] = field(default_factory=dict)
    trimmed: list[str] = field(default_factory=list)
    recalled_memory_ids: list[str] = field(default_factory=list)
    no_match: bool = True
    tools_included: int = 0
    tools_omitted: int = 0
    memory_omitted: int = 0
    budget_chars: dict[str, int] = field(default_factory=dict)
    budget_unit: str = "characters"
    event_order: str = "oldest_to_newest"

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
            "memory_omitted": getattr(self, "memory_omitted", 0),
            "budget_chars": self.budget_chars,
            "budget_unit": self.budget_unit,
            "event_order": self.event_order,
        }


class ContextComposer:
    """Compose provider context with deterministic character budgets, not token estimates."""

    def __init__(self, store: Store, tool_store=None):
        self.store = store
        self.tool_store = tool_store

    def member_persona(self, member_id: str) -> dict[str, Any]:
        row = self.store.db.execute("SELECT member_id, name, role, responsibilities, persona FROM members WHERE member_id=?", (member_id,)).fetchone()
        if not row:
            raise ValueError("member_not_found")
        return {
            "name": row["name"],
            "role": row["role"],
            "responsibilities": json.loads(row["responsibilities"] or "[]"),
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
        return [{"event_type": row["event_type"], "payload": json.loads(row["payload"]), "created_at": row["created_at"]} for row in reversed(rows)]

    @staticmethod
    def _compact_json_section(label: str, value: Any, limit: int) -> str:
        marker = "\n"
        minimum = len(label) + len(marker) + len('{"truncated":true}')
        if limit < minimum:
            raise ValueError("context_budget_too_small")
        compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(label) + len(marker) + len(compact) <= limit:
            return label + marker + compact
        if isinstance(value, dict):
            reduced = {"truncated": True, "keys": sorted(value.keys())}
        elif isinstance(value, list):
            reduced = {"truncated": True, "count": len(value), "tail": value[-1:]}
        else:
            reduced = {"truncated": True}
        while True:
            text = label + marker + json.dumps(reduced, ensure_ascii=False, separators=(",", ":"))
            if len(text) <= limit:
                return text
            if isinstance(reduced, dict) and "tail" in reduced:
                reduced.pop("tail")
            elif isinstance(reduced, dict) and "keys" in reduced:
                reduced.pop("keys")
            else:
                return label + marker + '{"truncated":true}'

    @staticmethod
    def _json_section(label: str, value: Any) -> str:
        return label + "\n" + json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _memory_section(memories: list[dict[str, Any]]) -> str:
        lines = ["【L3 相关记忆】（按权重召回；标注为参考的内容不是确定性指令）"]
        for item in memories:
            flag = "" if item["status"] in {"hot", "warm", "verified"} else f"[{item['status']}·仅供参考]"
            lines.append(f"- {flag}{item['content']}")
        return "\n".join(lines)

    @staticmethod
    def _persona_section(persona: dict[str, Any], limit: int) -> str:
        view = {key: value for key, value in persona.items() if value}
        label = "【L4 身份与职责】"
        full = ContextComposer._json_section(label, view)
        if len(full) <= limit:
            return full
        compact = {key: view[key] for key in ("name", "role") if view.get(key)}
        compact["truncated"] = True
        return ContextComposer._compact_json_section(label, compact, limit)

    def compose(self, member_id: str, instruction: str, *, task_id: str | None = None, memory: MemoryStore | None = None, environment: dict[str, Any] | None = None, budgets: dict[str, int] | None = None, include_tools: bool = False) -> tuple[str, ContextReport]:
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction_required")
        budget = dict(DEFAULT_BUDGETS)
        supplied = dict(budgets or {})
        window = supplied.pop("WINDOW", None)
        budget.update(supplied)
        for name, amount in budget.items():
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                raise ValueError("invalid_context_budget")
        if window is not None and (isinstance(window, bool) or not isinstance(window, int) or window <= 0):
            raise ValueError("invalid_context_window")

        decision_id = f"dec_{uuid.uuid4().hex}"
        report = ContextReport(member_id=member_id, task_id=task_id, decision_id=decision_id)
        persona = self.member_persona(member_id)
        task = self.task_package(task_id) if task_id else None
        events = self.recent_events(task_id, limit=5) if task_id else []
        memories: list[dict[str, Any]] = []
        if memory is not None:
            query_parts = [instruction.strip()]
            if task:
                query_parts.append(task.get("title") or "")
            memories = memory.recall_for_decision(" ".join(part for part in query_parts if part)[:200], limit=8, environment=environment, decision_id=decision_id)
        report.recalled_memory_ids = [item["memory_id"] for item in memories]
        report.no_match = not memories

        l0 = "【L0 当前指令】\n" + instruction.strip()
        layer_values: dict[str, str | None] = {"L1": None, "L2": None, "L3": None, "L4": None}
        if task:
            text = self._json_section("【L1 当前任务】", task)
            layer_values["L1"] = self._compact_json_section("【L1 当前任务】", task, budget["L1"]) if len(text) > budget["L1"] else text
            if len(text) > budget["L1"]:
                report.trimmed.append("L1")
        if events:
            text = self._json_section("【L2 近期事件】", events)
            layer_values["L2"] = self._compact_json_section("【L2 近期事件】", {"events": events}, budget["L2"]) if len(text) > budget["L2"] else text
            if len(text) > budget["L2"]:
                report.trimmed.append("L2")
        if memories:
            text = self._memory_section(memories)
            selected: list[dict[str, Any]] = []
            for item in memories:
                candidate = selected + [item]
                if len(self._memory_section(candidate)) <= budget["L3"]:
                    selected = candidate
                else:
                    report.trimmed.append("L3")
                    break
            if selected and len(selected) < len(memories):
                report.memory_omitted = len(memories) - len(selected)
            if selected:
                layer_values["L3"] = self._memory_section(selected)
        layer_values["L4"] = self._persona_section(persona, budget["L4"])
        if len(self._json_section("【L4 身份与职责】", {key: value for key, value in persona.items() if value})) > budget["L4"]:
            report.trimmed.append("L4")

        cards = []
        if include_tools and self.tool_store is not None:
            cards = self.tool_store.context_cards(member_id, instruction.strip(), limit=100)

        header_and_cards: list[str] = []
        if cards:
            used = len(_TOOL_HEADER)
            if used <= budget["TOOLS"]:
                header_and_cards.append(_TOOL_HEADER)
                for card in cards:
                    rendered = "- " + json.dumps(card, ensure_ascii=False, separators=(",", ":"))
                    if used + len(rendered) > budget["TOOLS"]:
                        report.tools_omitted += 1
                        continue
                    header_and_cards.append(rendered)
                    used += len(rendered)
                    report.tools_included += 1
            else:
                report.tools_omitted = len(cards)
            if report.tools_omitted:
                report.trimmed.append("TOOLS")

        def assemble() -> tuple[list[str], int]:
            sections = [l0]
            sections.extend(value for value in (layer_values["L1"], layer_values["L2"], layer_values["L3"]) if value)
            sections.extend(header_and_cards)
            if layer_values["L4"]:
                sections.append(layer_values["L4"])
            result = "\n\n".join(sections)
            return sections, len(result)

        sections, total_chars = assemble()
        if window is not None and total_chars > window:
            # Degrade in declared low-priority order; L0 is never changed.
            for key in ("L4", "L3", "L2", "L1", "TOOLS"):
                if total_chars <= window:
                    break
                if key == "TOOLS":
                    if header_and_cards:
                        report.tools_omitted += report.tools_included
                        report.tools_included = 0
                        header_and_cards = []
                        if "TOOLS" not in report.trimmed:
                            report.trimmed.append("TOOLS")
                elif layer_values[key] is not None:
                    layer_values[key] = None
                    if key not in report.trimmed:
                        report.trimmed.append(key)
                sections, total_chars = assemble()
            if total_chars > window:
                raise ValueError("context_budget_too_small")

        visible_memories = selected if memories and layer_values["L3"] is not None else []
        report.recalled_memory_ids = [item["memory_id"] for item in visible_memories]
        report.memory_omitted = len(memories) - len(visible_memories)

        for key, value in layer_values.items():
            if value is not None:
                report.layers[key] = len(value)
        report.layers["L0"] = len(l0)
        report.layers["TOOLS"] = sum(len(value) for value in header_and_cards)
        separators = max(0, len(sections) - 1) * 2
        report.budget_chars = {
            "F": len(l0),
            "M": sum(len(value) for key, value in layer_values.items() if key != "L0" and value is not None),
            "T": report.layers["TOOLS"],
            "S": separators,
            "total": total_chars,
            "window": window if window is not None else total_chars,
            "fits_window": window is None or total_chars <= window,
        }
        return "\n\n".join(sections), report
