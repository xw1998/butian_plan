from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from butian.runtime.service import ButianService


def _base_handoff(task_id, revision, **overrides):
    payload = {
        "task_id": task_id,
        "task_revision": revision,
        "member_id": "m_h",
        "status": "done",
        "summary": "工作完成",
        "evidence_ids": [],
        "next_action": "交付",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


class HandoffGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.service = ButianService(Path(self._tmp.name))
        self.service.add_member("m_h", "执行者")
        self.service.add_member("m_d", "派活者")
        session = self.service.create_session("team", "m_d")
        self.task_id = self.service.store.create_task(session["session_id"], "任务A", "m_h")
        self.service.store.transition_task(self.task_id, 1, "ready", "hd-ready")
        self.service.store.transition_task(self.task_id, 2, "claimed", "hd-claim")
        self.service.store.transition_task(self.task_id, 3, "running", "hd-run")
        self.service.store.transition_task(self.task_id, 4, "review", "hd-review")

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def _record_evidence(self, tag):
        path = Path(self._tmp.name) / f"ev_{tag}.txt"
        path.write_text(f"evidence {tag}", encoding="utf-8")
        return self.service.record_evidence(str(path), owner_member_id="m_h", actor_member_id="m_h")

    def test_done_without_handoff_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.service.store.transition_task(self.task_id, 5, "done", "hd-done-nohandoff")
        self.assertIn("handoff_required_for_done", str(ctx.exception))
        status = self.service.store.db.execute("SELECT status FROM tasks WHERE task_id=?", (self.task_id,)).fetchone()[0]
        self.assertEqual(status, "review")

    def test_done_with_empty_evidence_handoff_rejected(self):
        self.service.submit_handoff(_base_handoff(self.task_id, 5, evidence_ids=[]))
        with self.assertRaises(ValueError) as ctx:
            self.service.store.transition_task(self.task_id, 5, "done", "hd-done-noev")
        self.assertIn("handoff_evidence_required_for_done", str(ctx.exception))

    def test_done_with_nonexistent_member_rejected(self):
        # 提交侧已直接拒绝不存在成员
        evidence_id = self._record_evidence("m1")
        with self.assertRaises(ValueError) as ctx:
            self.service.submit_handoff(_base_handoff(self.task_id, 5, member_id="ghost", evidence_ids=[evidence_id]))
        self.assertIn("member_not_found", str(ctx.exception))
        # 直插 handoffs 表模拟绕过提交口，状态机门槛仍要拦截
        import json as _json
        import time as _time
        payload = _base_handoff(self.task_id, 5, member_id="ghost", evidence_ids=[evidence_id])
        self.service.store.db.execute("INSERT OR REPLACE INTO handoffs VALUES (?,?,?,?,?,?)", (self.task_id, 5, "ghost", "done", _json.dumps(payload, ensure_ascii=False), _time.time()))
        with self.assertRaises(ValueError) as ctx:
            self.service.store.transition_task(self.task_id, 5, "done", "hd-done-ghost")
        self.assertIn("handoff_member_not_found", str(ctx.exception))

    def test_done_with_missing_evidence_record_rejected(self):
        self.service.submit_handoff(_base_handoff(self.task_id, 5, evidence_ids=["ev_nope"]))
        with self.assertRaises(ValueError) as ctx:
            self.service.store.transition_task(self.task_id, 5, "done", "hd-done-badev")
        self.assertIn("handoff_evidence_not_found:ev_nope", str(ctx.exception))

    def test_done_with_valid_handoff_succeeds(self):
        evidence_id = self._record_evidence("ok1")
        self.service.submit_handoff(_base_handoff(self.task_id, 5, evidence_ids=[evidence_id]))
        self.service.store.transition_task(self.task_id, 5, "done", "hd-done-ok")
        status = self.service.store.db.execute("SELECT status, revision FROM tasks WHERE task_id=?", (self.task_id,)).fetchone()
        self.assertEqual(status[0], "done")
        self.assertEqual(status[1], 6)

    def test_submit_handoff_stale_revision_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.service.submit_handoff(_base_handoff(self.task_id, 3))
        self.assertIn("handoff_stale_revision", str(ctx.exception))
        count = self.service.store.db.execute("SELECT COUNT(*) FROM handoffs WHERE task_id=?", (self.task_id,)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_submit_handoff_nonexistent_member_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.service.submit_handoff(_base_handoff(self.task_id, 5, member_id="ghost"))
        self.assertIn("member_not_found", str(ctx.exception))

    def test_submit_handoff_task_not_found(self):
        with self.assertRaises(ValueError) as ctx:
            self.service.submit_handoff(_base_handoff("task_nope", 1))
        self.assertIn("task_not_found", str(ctx.exception))

    def test_submit_handoff_empty_fields_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.service.submit_handoff(_base_handoff(self.task_id, 5, summary="  "))
        self.assertIn("handoff_field_empty:summary", str(ctx.exception))

    def test_submit_handoff_bad_types_rejected(self):
        with self.assertRaises(ValueError):
            self.service.submit_handoff(_base_handoff(self.task_id, 5, evidence_ids="ev1"))
        with self.assertRaises(ValueError):
            self.service.submit_handoff(_base_handoff(self.task_id, 5, confidence="high"))


if __name__ == "__main__":
    unittest.main()
