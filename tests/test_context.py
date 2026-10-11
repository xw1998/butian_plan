from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from butian.runtime.service import ButianService


class ContextComposerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self._tmp.name)
        self.service = ButianService(self.home)
        self.store = self.service.store
        self.store.add_member("m_ctx", "测试成员", role="engineer", responsibilities=["嵌入式开发"], provider_id="local-command", model_id="local-command")

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def _make_task(self, title="实现串口驱动", payload=None):
        session_id = self.store.create_session("solo", "m_ctx", None, {})
        task_id = self.store.create_task(session_id, title, assignee_id="m_ctx", payload=payload or {"acceptance": "驱动可收发"})
        return task_id

    def test_compose_contains_all_layers(self):
        task_id = self._make_task()
        composed, report = self.service.compose_context("m_ctx", "请实现串口驱动", task_id=task_id)
        for marker in ("【L0 当前指令】", "【L1 当前任务】", "【L4 身份与职责】"):
            self.assertIn(marker, composed)
        self.assertIn("请实现串口驱动", composed)
        self.assertIn("实现串口驱动", composed)
        self.assertIn("测试成员", composed)
        self.assertEqual(report.member_id, "m_ctx")
        self.assertEqual(report.task_id, task_id)
        self.assertTrue(report.decision_id.startswith("dec_"))
        self.assertTrue(report.no_match)  # 独立记忆库为空 → 无召回

    def test_persona_column_present(self):
        row = self.store.db.execute("SELECT persona FROM members WHERE member_id='m_ctx'").fetchone()
        self.assertIn("persona", row.keys())

    def _evidence_event(self, tag):
        return self.store.append_event("m_ctx", "evidence_recorded", {"tag": tag}, f"op_ctx_{tag}")

    def test_memory_recall_included_in_l3(self):
        event_id = self._evidence_event("warm1")
        memory = self.service.memory("m_ctx")
        memory.put("mem_1", "lesson", "串口驱动必须先配置时钟再配置引脚", status="warm", source_event_ids=[event_id])
        memory.close()
        composed, report = self.service.compose_context("m_ctx", "配置串口驱动时钟")
        self.assertIn("【L3 相关记忆】", composed)
        self.assertIn("串口驱动必须先配置时钟", composed)
        self.assertEqual(report.recalled_memory_ids, ["mem_1"])
        self.assertFalse(report.no_match)

    def test_invalidated_memory_not_recalled(self):
        memory = self.service.memory("m_ctx")
        memory.put("mem_bad", "lesson", "过时结论串口驱动不需要时钟", status="invalidated")
        memory.close()
        composed, report = self.service.compose_context("m_ctx", "配置串口驱动时钟")
        self.assertNotIn("过时结论", composed)
        self.assertTrue(report.no_match)

    def test_candidate_memory_marked_as_reference(self):
        memory = self.service.memory("m_ctx")
        memory.put("mem_c", "guess", "候选推测串口时钟来自 HSI", status="candidate")
        memory.close()
        composed, _ = self.service.compose_context("m_ctx", "串口时钟来自哪里")
        self.assertIn("[candidate", composed)

    def test_recall_logged_with_decision_id(self):
        event_id = self._evidence_event("hot2")
        memory = self.service.memory("m_ctx")
        memory.put("mem_2", "fact", "板子使用外部 25M 晶振", status="hot", source_event_ids=[event_id])
        memory.close()
        _, report = self.service.compose_context("m_ctx", "晶振频率是多少")
        memory = self.service.memory("m_ctx")
        rows = memory.db.execute("SELECT decision_id, memory_id FROM memory_recall_log WHERE decision_id=?", (report.decision_id,)).fetchall()
        memory.close()
        self.assertEqual([row["memory_id"] for row in rows], ["mem_2"])

    def test_budget_trims_only_low_layers(self):
        task_id = self._make_task(payload={"blob": "x" * 5000})
        composed, report = self.service.compose_context("m_ctx", "做任务", task_id=task_id, budgets={"L1": 200})
        self.assertIn("L1", report.trimmed)
        self.assertIn("【L0 当前指令】", composed)  # L0 永不裁剪
        self.assertIn("做任务", composed)

    def test_instruction_required(self):
        with self.assertRaises(ValueError):
            self.service.compose_context("m_ctx", "   ")

    def test_member_not_found(self):
        with self.assertRaises(ValueError):
            self.service.compose_context("no_such_member", "hi")

    def test_events_layer_included_when_events_exist(self):
        task_id = self._make_task()
        self.store.append_event(task_id, "task_started", {"by": "m_ctx"}, "op_test_ctx_evt_1")
        composed, report = self.service.compose_context("m_ctx", "继续任务", task_id=task_id)
        self.assertIn("【L2 近期事件】", composed)
        self.assertIn("task_started", composed)


class ProviderTaskContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self._tmp.name)
        # local-command provider：回显收到的完整上下文
        (self.home / "providers.json").write_text(json.dumps({"providers": [{"type": "local-command", "command": [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]}]}), encoding="utf-8")
        self.service = ButianService(self.home)
        self.service.store.add_member("m_p", "执行者", provider_id="local-command", model_id="local-command")

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def test_start_uses_composed_context_and_persists_it(self):
        result = self.service.start_provider_task("m_p", "执行任务指令")
        self.assertEqual(result["status"], "running")
        ctx = result["context_report"]
        self.assertEqual(ctx["member_id"], "m_p")
        # consume 不传 prompt 时复用持久化的组合上下文
        consumed = self.service.consume_provider_task(result["provider_session_id"])
        self.assertEqual(consumed["status"], "completed")
        self.assertIn("【L0 当前指令】", consumed["text"])
        self.assertIn("执行任务指令", consumed["text"])
        self.assertIn("【L4 身份与职责】", consumed["text"])

    def test_context_report_audited_via_event(self):
        task_id = self.service.store.create_task(self.service.store.create_session("solo", "m_p", None, {}), "标题", assignee_id="m_p")
        result = self.service.start_provider_task("m_p", "干活", task_id=task_id)
        rows = self.service.store.db.execute("SELECT event_type, payload FROM events WHERE aggregate_id=?", (task_id,)).fetchall()
        types = [row["event_type"] for row in rows]
        self.assertIn("provider_context_composed", types)
        payload = json.loads(next(row["payload"] for row in rows if row["event_type"] == "provider_context_composed"))
        self.assertEqual(payload["decision_id"], result["context_report"]["decision_id"])


if __name__ == "__main__":
    unittest.main()


class NaturalLanguageGoalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self._tmp.name)
        (self.home / "providers.json").write_text(json.dumps({"providers": [{"type": "local-command", "command": [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]}]}), encoding="utf-8")
        self.service = ButianService(self.home)
        self.service.store.add_member("m_goal", "目标执行者", provider_id="local-command", model_id="local-command")

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def test_submit_goal_creates_task_and_starts_real_provider(self):
        result = self.service.submit_goal("分析这个目标", member_id="m_goal")
        self.assertEqual(result["session"]["mode"], "solo")
        self.assertEqual(result["assignee_id"], "m_goal")
        self.assertEqual(result["execution"]["status"], "running")
        task = next(t for t in self.service.store.list_tasks() if t["task_id"] == result["task_id"])
        self.assertEqual(task["title"], "分析这个目标")
        consumed = self.service.consume_provider_task(result["execution"]["provider_session_id"])
        self.assertEqual(consumed["status"], "completed")
        self.assertIn("分析这个目标", consumed["text"])

    def test_submit_goal_rejects_empty_goal(self):
        with self.assertRaisesRegex(ValueError, "goal_required"):
            self.service.submit_goal("  ", member_id="m_goal")


def test_window_budget_preserves_instruction_and_reports_exact_characters(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m_window", "窗口成员", role="工程师", responsibilities=["职责" * 200], persona={"persona": "人格" * 1000})
        task_id = service.store.create_task(service.store.create_session("solo", "m_window", None, {}), "任务", assignee_id="m_window", payload={"blob": "任务" * 1000})
        instruction = "保持完整指令：" + "重要" * 100
        composed, report = service.compose_context("m_window", instruction, task_id=task_id, budgets={"WINDOW": 320, "L1": 1200, "L2": 1200, "L3": 1200, "L4": 1200, "TOOLS": 1200})
        assert "【L0 当前指令】\n" + instruction in composed
        assert report.budget_chars["total"] == len(composed)
        assert report.budget_chars["F"] + report.budget_chars["M"] + report.budget_chars["T"] + report.budget_chars["S"] == len(composed)
        assert len(composed) <= 320
        assert report.budget_chars["fits_window"] is True
    finally:
        service.close()


def test_window_too_small_fails_without_truncating_l0(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m_tiny", "小窗口")
        with pytest.raises(ValueError, match="context_budget_too_small"):
            service.compose_context("m_tiny", "不可裁剪" * 40, budgets={"WINDOW": 20})
    finally:
        service.close()


def test_json_sections_remain_valid_with_unicode_truncation(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m_json", "中文" * 60, role="角色" * 60, responsibilities=["职责" * 60], persona={"persona": "人格" * 60})
        task_id = service.store.create_task(service.store.create_session("solo", "m_json", None, {}), "标题" * 80, assignee_id="m_json", payload={"内容": "值" * 500})
        composed, report = service.compose_context("m_json", "指令", task_id=task_id, budgets={"L1": 80, "L4": 80})
        for label in ("【L1 当前任务】", "【L4 身份与职责】"):
            section = composed.split(label + "\n", 1)[1].split("\n\n", 1)[0]
            assert json.loads(section)["truncated"] is True
        assert {"L1", "L4"}.issubset(report.trimmed)
    finally:
        service.close()


def test_recent_events_are_oldest_to_newest(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m_events", "事件成员")
        task_id = service.store.create_task(service.store.create_session("solo", "m_events", None, {}), "事件任务", assignee_id="m_events")
        for value in range(3):
            service.store.append_event(task_id, "test_event", {"index": value}, f"event-{value}")
        from butian.runtime.context import ContextComposer
        events = ContextComposer(service.store, service.tools).recent_events(task_id, limit=3)
        assert [event["payload"]["index"] for event in events] == [0, 1, 2]
    finally:
        service.close()
