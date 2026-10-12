from __future__ import annotations

import json
import sys
import tempfile
import unittest
import threading
from urllib import request as urlrequest, error as urlerror
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from butian.runtime.service import ButianService


def _svc():
    home = Path(tempfile.mkdtemp(prefix="butian_team_"))
    return ButianService(home), home


def _make_task_ready(svc, session_id, title="t"):
    task_id = svc.store.create_task(session_id, title)
    svc.store.transition_task(task_id, 1, "ready", f"op_ready_{task_id}")
    svc.store.transition_task(task_id, 2, "claimed", f"op_claim_{task_id}")
    svc.store.transition_task(task_id, 3, "running", f"op_run_{task_id}")
    svc.store.transition_task(task_id, 4, "review", f"op_review_{task_id}")
    return task_id


_EVIDENCE_SEQ = [0]

def _add_evidence(svc, member_id, task_id, revision):
    _EVIDENCE_SEQ[0] += 1
    path = svc.home / f"ev_{task_id}_{_EVIDENCE_SEQ[0]}.txt"
    path.write_text(f"evidence {task_id} #{_EVIDENCE_SEQ[0]}", encoding="utf-8")
    evidence_id = svc.record_evidence(str(path), owner_member_id=member_id, actor_member_id=member_id)
    svc.submit_handoff({"task_id": task_id, "task_revision": revision, "member_id": member_id,
                        "status": "done", "summary": "s", "next_action": "n",
                        "evidence_ids": [evidence_id], "confidence": 0.9})
    return evidence_id


class TeamOrchestrationTests(unittest.TestCase):
    def test_invalid_mode_rejected(self):
        svc, _ = _svc()
        try:
            with self.assertRaises(ValueError) as ctx:
                svc.create_session("group")
            self.assertIn("invalid_session_mode", str(ctx.exception))
        finally:
            svc.close()

class TeamGoalHttpTests(unittest.TestCase):
    def test_submit_and_run_team_routes_with_validation_error(self):
        from butian.runtime import http_api
        home = Path(tempfile.mkdtemp(prefix="butian_team_http_"))
        command = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
        (home / "providers.json").write_text(json.dumps({"providers": [{"type": "local-command", "command": command}]}), encoding="utf-8")
        svc = ButianService(home)
        svc.add_member("m0", "派活者")
        svc.add_member("m1", "执行者", provider_id="local-command", model_id="local-command")
        svc.store.set_member_token("m0", "secret-m0")
        svc.store.set_member_token("m1", "secret-m1")
        http_api.ApiHandler.service = svc
        http_api.ApiHandler.experiments = None
        http_api.ApiHandler.web_root = Path(__file__).resolve().parents[1] / "web"
        server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        def post(path, payload):
            req = urlrequest.Request(base + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "X-Butian-Member": "m0", "X-Butian-Token": "secret-m0"}, method="POST")
            try:
                with urlrequest.urlopen(req) as response:
                    return response.status, json.loads(response.read())
            except urlerror.HTTPError as exc:
                return exc.code, json.loads(exc.read())
        try:
            status, created = post("/api/goals/submit", {"goal": "目标", "mode": "team", "member_id": "m0", "member_ids": ["m1"]})
            self.assertEqual(status, 201)
            self.assertEqual(len(created["data"]["children"]), 1)
            self.assertEqual(created["data"]["children"][0]["member_id"], "m1")
            status, created = post("/api/goals/submit", {"goal": "联合评估", "mode": "team", "member_id": "m0", "member_ids": ["m1"], "subtasks": ["验证数据"]})
            self.assertEqual(status, 201)
            task_id = created["data"]["task_id"]
            status, result = post("/api/goals/run-team", {"task_id": task_id, "actor_member_id": "m0"})
            self.assertEqual(status, 200)
            self.assertEqual(result["data"]["status"], "awaiting_child_reviews")
        finally:
            server.shutdown()
            server.server_close()
            svc.close()

class TeamExecutionAndReviewTests(unittest.TestCase):
    def test_team_session_requires_dispatcher(self):
        svc, _ = _svc()
        try:
            with self.assertRaises(ValueError) as ctx:
                svc.create_session("team")
            self.assertIn("dispatcher_required", str(ctx.exception))
        finally:
            svc.close()

    def test_team_dispatcher_default_from_current_member(self):
        svc, _ = _svc()
        try:
            svc.add_member("m1", "成员一")
            result = svc.create_session("team", current_member_id="m1")
            self.assertEqual(result["dispatcher_member_id"], "m1")
        finally:
            svc.close()

    def test_team_dispatcher_config_wins_but_explicit_overrides(self):
        svc, _ = _svc()
        try:
            svc.add_member("m1", "一"); svc.add_member("m2", "二")
            svc.set_dispatcher_config("m2")
            # 未显式指定时用后台配置
            self.assertEqual(svc.create_session("team", current_member_id="m1")["dispatcher_member_id"], "m2")
            # 显式指定优先于后台配置
            self.assertEqual(svc.create_session("team", "m1")["dispatcher_member_id"], "m1")
            # 配置指向不存在的成员要报错，不静默回退
            svc.store.set_setting("dispatcher_member_id", "ghost")
            with self.assertRaises(ValueError) as ctx:
                svc.create_session("team")
            self.assertIn("dispatcher_member_not_found:ghost", str(ctx.exception))
        finally:
            svc.close()

    def test_solo_to_team_switch_keeps_tasks(self):
        svc, _ = _svc()
        try:
            svc.add_member("m1", "一")
            s = svc.create_session("solo", current_member_id="m1")
            sid = s["session_id"]
            task_id = svc.store.create_task(sid, "任务")
            svc.store.transition_task(task_id, 1, "ready", "sw_r")
            result = svc.switch_session_mode(sid, "team", current_member_id="m1")
            self.assertTrue(result["changed"])
            self.assertEqual(result["running_tasks_preserved"], 1)
            session = svc.store.session(sid)
            self.assertEqual(session["mode"], "team")
            self.assertEqual(session["dispatcher_member_id"], "m1")
            # 任务还在
            tasks = svc.store.list_tasks()
            self.assertEqual(len(tasks), 1)
        finally:
            svc.close()

    def test_team_to_solo_running_tasks_not_cancelled(self):
        svc, _ = _svc()
        try:
            svc.add_member("m1", "一")
            sid = svc.create_session("team", current_member_id="m1")["session_id"]
            task_id = _make_task_ready(svc, sid)
            result = svc.switch_session_mode(sid, "solo")
            self.assertEqual(result["running_tasks_preserved"], 1)
            self.assertEqual(svc.store.session(sid)["mode"], "solo")
            # 运行中任务原状态保留（review），未被取消
            task = next(t for t in svc.store.list_tasks() if t["task_id"] == task_id)
            self.assertEqual(task["status"], "review")
        finally:
            svc.close()

    def test_task_tree_structure(self):
        svc, _ = _svc()
        try:
            svc.add_member("m1", "一")
            sid = svc.create_session("team", current_member_id="m1")["session_id"]
            parent = svc.store.create_task(sid, "父")
            c1 = svc.store.create_task(sid, "子1", parent_id=parent)
            c2 = svc.store.create_task(sid, "子2", parent_id=parent)
            tree = svc.task_tree(sid)
            self.assertEqual(len(tree), 1)
            self.assertEqual(tree[0]["task_id"], parent)
            self.assertEqual({c["task_id"] for c in tree[0]["children"]}, {c1, c2})
        finally:
            svc.close()

    def test_parent_done_blocked_by_open_children(self):
        svc, _ = _svc()
        try:
            svc.add_member("m1", "一")
            sid = svc.create_session("team", current_member_id="m1")["session_id"]
            parent = _make_task_ready(svc, sid, "父")
            child = svc.store.create_task(sid, "子", parent_id=parent)
            _add_evidence(svc, "m1", parent, 5)
            with self.assertRaises(ValueError) as ctx:
                svc.review_task(parent, 5, "approve", reviewer_member_id="m1")
            self.assertIn(f"children_not_done:{child}", str(ctx.exception))
        finally:
            svc.close()

    def test_review_approve_and_rework_flow(self):
        svc, _ = _svc()
        try:
            svc.add_member("m1", "一")
            sid = svc.create_session("team", current_member_id="m1")["session_id"]
            task_id = _make_task_ready(svc, sid)
            _add_evidence(svc, "m1", task_id, 5)
            # 返工：review→ready
            r = svc.review_task(task_id, 5, "rework", reviewer_member_id="m1", notes="不合格")
            self.assertEqual(r["status"], "ready"); self.assertEqual(r["rework_count"], 1)
            self.assertEqual(r["execution"]["reason"], "provider_unavailable")
            # 重新走完到 review
            for rev, target in ((6, "claimed"), (7, "running"), (8, "review")):
                svc.store.transition_task(task_id, rev, target, f"op_{task_id}_{rev}")
            _add_evidence(svc, "m1", task_id, 9)
            r = svc.review_task(task_id, 9, "approve", reviewer_member_id="m1")
            self.assertEqual(r["status"], "done")
        finally:
            svc.close()

    def test_rework_limit_enforced(self):
        svc, _ = _svc()
        try:
            svc.add_member("m1", "一")
            sid = svc.create_session("team", current_member_id="m1")["session_id"]
            task_id = _make_task_ready(svc, sid)
            rev = 5
            for i in range(3):  # rework_limit=3
                svc.review_task(task_id, rev, "rework", reviewer_member_id="m1")
                rev += 1
                for target in ("claimed", "running", "review"):
                    svc.store.transition_task(task_id, rev, target, f"op_{task_id}_{rev}_{target}")
                    rev += 1
            with self.assertRaises(ValueError) as ctx:
                svc.review_task(task_id, rev, "rework", reviewer_member_id="m1")
            self.assertIn("rework_limit_exceeded", str(ctx.exception))
        finally:
            svc.close()

    def test_review_verdict_invalid(self):
        svc, _ = _svc()
        try:
            svc.add_member("m1", "一")
            sid = svc.create_session("team", current_member_id="m1")["session_id"]
            task_id = _make_task_ready(svc, sid)
            with self.assertRaises(ValueError) as ctx:
                svc.review_task(task_id, 5, "maybe")
            self.assertIn("invalid_review_verdict", str(ctx.exception))
        finally:
            svc.close()




class NaturalLanguageTeamGoalTests(unittest.TestCase):
    def test_team_goal_dispatches_children_to_requested_members(self):
        home = Path(tempfile.mkdtemp(prefix="butian_team_goal_"))
        (home / "providers.json").write_text('{"providers":[{"type":"local-command","command":["python","-c","import sys; sys.stdout.write(sys.stdin.read())"]}]}', encoding="utf-8")
        svc = ButianService(home)
        try:
            svc.add_member("m0", "派活者", provider_id="local-command", model_id="local-command")
            svc.add_member("m1", "执行一", provider_id="local-command", model_id="local-command")
            svc.add_member("m2", "执行二", provider_id="local-command", model_id="local-command")
            result = svc.submit_goal("完成联合评估", mode="team", member_id="m0", member_ids=["m1", "m2"], subtasks=["分析信号", "审核边界"])
            self.assertEqual(result["status"], "dispatched")
            self.assertEqual([x["member_id"] for x in result["children"]], ["m1", "m2"])
            tasks = {x["task_id"]: x for x in svc.store.list_tasks()}
            self.assertEqual(tasks[result["task_id"]]["status"], "todo")
            self.assertEqual([tasks[x["task_id"]]["status"] for x in result["children"]], ["running", "running"])
            events = svc.store.db.execute("SELECT event_type FROM events WHERE aggregate_id=?", (result["task_id"],)).fetchall()
            self.assertIn("team_goal_dispatched", [x["event_type"] for x in events])
        finally:
            svc.close()

    def test_team_goal_rejects_mismatched_explicit_plan(self):
        home = Path(tempfile.mkdtemp(prefix="butian_team_goal_bad_"))
        svc = ButianService(home)
        try:
            svc.add_member("m0", "派活者")
            svc.add_member("m1", "执行一", provider_id="local-command", model_id="local-command")
            (home / "providers.json").write_text(json.dumps({"providers": [{"type": "local-command", "command": [sys.executable, "-c", "pass"]}]}), encoding="utf-8")
            svc.close()
            svc = ButianService(home)
            with self.assertRaisesRegex(ValueError, "subtasks_member_count_mismatch"):
                svc.submit_goal("目标", mode="team", member_id="m0", member_ids=["m1"], subtasks=["一", "二"])
            self.assertEqual(svc.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(svc.store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        finally:
            svc.close()

    def test_team_goal_generates_deterministic_plan_when_omitted(self):
        home = Path(tempfile.mkdtemp(prefix="butian_team_goal_auto_"))
        command = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
        (home / "providers.json").write_text(json.dumps({"providers": [{"type": "local-command", "command": command}]}), encoding="utf-8")
        svc = ButianService(home)
        try:
            svc.add_member("m0", "派活者")
            svc.add_member("m1", "执行一", provider_id="local-command", model_id="local-command")
            result = svc.submit_goal("目标", mode="team", member_id="m0", member_ids=["m1"])
            self.assertEqual(result["status"], "dispatched")
            self.assertEqual(len(result["children"]), 1)
            child = next(task for task in svc.store.list_tasks() if task["task_id"] == result["children"][0]["task_id"])
            self.assertIn("目标", child["title"])
            self.assertEqual(svc.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual(svc.store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
        finally:
            svc.close()

    def test_team_goal_rejects_unavailable_provider_before_writing(self):
        svc, _ = _svc()
        try:
            svc.add_member("m0", "派活者")
            svc.add_member("m1", "执行一")
            with self.assertRaisesRegex(ValueError, "provider_unavailable"):
                svc.submit_goal("目标", mode="team", member_id="m0", member_ids=["m1"], subtasks=["具体工作"])
            self.assertEqual(svc.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(svc.store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        finally:
            svc.close()


class TeamGoalExecutionTests(unittest.TestCase):
    def test_run_team_goal_consumes_outputs_and_moves_root_to_review(self):
        home = Path(tempfile.mkdtemp(prefix="butian_team_run_"))
        command = [sys.executable, "-c", "import sys; sys.stdout.write('RESULT:' + sys.stdin.read())"]
        (home / "providers.json").write_text(json.dumps({"providers": [{"type": "local-command", "command": command}]}), encoding="utf-8")
        svc = ButianService(home)
        try:
            svc.add_member("m0", "派活者")
            svc.add_member("m1", "执行一", provider_id="local-command", model_id="local-command")
            result = svc.submit_goal("联合目标", mode="team", member_id="m0", member_ids=["m1"], subtasks=["子目标甲"])
            run = svc.run_team_goal(result["task_id"])
            self.assertEqual(run["children"][0]["status"], "review")
            child_id = run["children"][0]["task_id"]
            child = next(t for t in svc.store.list_tasks() if t["task_id"] == child_id)
            child_payload = json.loads(child["payload"])
            self.assertTrue(Path(child_payload["artifact_path"]).is_file())
            self.assertEqual(svc.store.verify_evidence(child_payload["evidence_id"], actor_member_id="m1")["status"], "valid")
            self.assertEqual(child["status"], "review")
            # 子任务必须经派活者审核后，父任务才能进入最终审核。
            svc.review_task(child_id, child["revision"], "approve", reviewer_member_id="m0")
            run = svc.run_team_goal(result["task_id"])
            self.assertEqual(run["status"], "review")
            root = next(t for t in svc.store.list_tasks() if t["task_id"] == result["task_id"])
            self.assertEqual(root["status"], "review")
            root_payload = json.loads(root["payload"])
            self.assertIn("子目标甲", root_payload["results"][0]["output"])
            self.assertTrue(root_payload["evidence_id"])
            self.assertTrue(svc.store.db.execute("SELECT 1 FROM handoffs WHERE task_id=? AND revision=?", (result["task_id"], root["revision"])).fetchone())
            done = svc.review_task(result["task_id"], root["revision"], "approve", reviewer_member_id="m0")
            self.assertEqual(done["status"], "done")
            self.assertEqual(svc.run_team_goal(result["task_id"])["status"], "done")
        finally:
            svc.close()

if __name__ == "__main__":
    unittest.main()


def test_task_heartbeat_and_cancel_http_routes():
    from butian.runtime import http_api
    home = Path(tempfile.mkdtemp(prefix="butian_lifecycle_http_"))
    service = ButianService(home)
    service.add_member("worker", "Worker")
    service.store.set_member_token("worker", "secret-worker")
    session_id = service.store.create_session("solo", None, None, {})
    task_id = service.store.create_task(session_id, "HTTP lifecycle", assignee_id="worker")
    service.store.transition_task(task_id, 1, "ready", "http-ready")
    lease = service.store.claim_task(task_id, "worker", lease_seconds=1)
    http_api.ApiHandler.service = service
    http_api.ApiHandler.experiments = None
    http_api.ApiHandler.web_root = Path(__file__).resolve().parents[1] / "web"
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    def post(path, payload):
        req = urlrequest.Request(base + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "X-Butian-Member": "worker", "X-Butian-Token": "secret-worker"}, method="POST")
        try:
            with urlrequest.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urlerror.HTTPError as exc:
            return exc.code, json.loads(exc.read())
    try:
        status, heartbeat = post(f"/api/tasks/{task_id}/heartbeat", {"lease_id": lease["lease_id"], "lease_seconds": 300, "actor_member_id": "worker"})
        assert status == 200 and heartbeat["data"]["ok"] is True
        status, missing = post(f"/api/tasks/{task_id}/heartbeat", {"actor_member_id": "worker"})
        assert status == 400 and missing["error"] == "missing_field"
        current_revision = service.store.db.execute("SELECT revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()["revision"]
        status, cancelled = post(f"/api/tasks/{task_id}/cancel", {"revision": current_revision, "actor_member_id": "worker"})
        assert status == 200, cancelled
        assert cancelled["data"]["status"] == "cancelled"
        status, invalid_revision = post(f"/api/tasks/{task_id}/cancel", {"revision": current_revision, "actor_member_id": "worker"})
        assert status == 400 and invalid_revision["error"] == "revision_conflict"
        provider_session_id = service.store.create_provider_session("worker", "manual")
        status, provider_cancel = post("/api/providers/cancel", {"provider_session_id": provider_session_id, "actor_member_id": "worker"})
        assert status == 200 and provider_cancel["data"]["status"] == "cancelled"
        status, missing_provider = post("/api/providers/cancel", {"actor_member_id": "worker"})
        assert status == 400 and missing_provider["error"] == "missing_field"
    finally:
        server.shutdown()
        server.server_close()
        service.close()


def test_team_goal_uses_sqlite_row_assignee_when_child_completes(tmp_path):
    (tmp_path / "providers.json").write_text(json.dumps({"providers": [{"type": "local-command", "command": [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]}]}), encoding="utf-8")
    service = ButianService(tmp_path)
    try:
        service.add_member("dispatcher", "派活者", provider_id="local-command", model_id="local-command")
        service.add_member("worker", "执行者", provider_id="local-command", model_id="local-command")
        result = service.submit_goal("团队验证", mode="team", member_id="dispatcher", member_ids=["worker"], subtasks=["完成实际验证"])
        child = result["children"][0]
        session = service.store.db.execute("SELECT provider_session_id FROM provider_sessions WHERE task_id=?", (child["task_id"],)).fetchone()
        output = service.run_team_goal(result["task_id"])
        assert output["status"] in {"review", "awaiting_child_reviews"}
    finally:
        service.close()


def test_real_provider_rework_receives_review_notes(tmp_path):
    command = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
    (tmp_path / "providers.json").write_text(json.dumps({"providers": [{"type": "local-command", "command": command}]}), encoding="utf-8")
    service = ButianService(tmp_path)
    try:
        service.add_member("dispatcher", "Dispatcher")
        service.add_member("worker", "Worker", provider_id="local-command", model_id="local-command")
        goal = service.submit_goal("review-loop", mode="team", member_id="dispatcher", member_ids=["worker"], subtasks=["initial instruction"])
        service.run_team_goal(goal["task_id"])
        child_id = goal["children"][0]["task_id"]
        child = next(t for t in service.store.list_tasks() if t["task_id"] == child_id)
        service.review_task(child_id, child["revision"], "rework", reviewer_member_id="dispatcher", notes="REWORK_REQUIRED_unique_add_boundary_evidence")
        rerun = service.run_team_goal(goal["task_id"])
        assert "REWORK_REQUIRED_unique_add_boundary_evidence" in rerun["children"][0]["text"]
        child = next(t for t in service.store.list_tasks() if t["task_id"] == child_id)
        service.review_task(child_id, child["revision"], "approve", reviewer_member_id="dispatcher")
        assert service.run_team_goal(goal["task_id"])["status"] == "review"
    finally:
        service.close()


def test_real_solo_provider_error_does_not_leave_task_running(tmp_path):
    command = [sys.executable, "-c", "import sys; sys.exit(7)"]
    (tmp_path / "providers.json").write_text(json.dumps({"providers": [{"type": "local-command", "command": command}]}), encoding="utf-8")
    service = ButianService(tmp_path)
    try:
        service.add_member("worker", "Worker", provider_id="local-command", model_id="local-command")
        goal = service.submit_goal("failure", member_id="worker")
        result = service.consume_provider_task(goal["execution"]["provider_session_id"])
        assert result["status"] == "unknown"
        task = service.store.db.execute("SELECT status,payload,lease_id FROM tasks WHERE task_id=?", (goal["task_id"],)).fetchone()
        assert task["status"] == "unknown"
        assert json.loads(task["payload"])["reason"] == "exit_code:7"
        assert task["lease_id"] is None
    finally:
        service.close()


def test_real_team_root_rework_runs_provider_instead_of_reusing_aggregate(tmp_path):
    command = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
    (tmp_path / "providers.json").write_text(json.dumps({"providers": [{"type": "local-command", "command": command}]}), encoding="utf-8")
    service = ButianService(tmp_path)
    try:
        for member in ("dispatcher", "worker"):
            service.add_member(member, member, provider_id="local-command", model_id="local-command")
        goal = service.submit_goal("root-review", mode="team", member_id="dispatcher", member_ids=["worker"], subtasks=["child-instruction"])
        service.run_team_goal(goal["task_id"])
        child = next(t for t in service.store.list_tasks() if t["parent_id"] == goal["task_id"])
        service.review_task(child["task_id"], child["revision"], "approve", reviewer_member_id="dispatcher")
        service.run_team_goal(goal["task_id"])
        root = next(t for t in service.store.list_tasks() if t["task_id"] == goal["task_id"])
        rework = service.review_task(root["task_id"], root["revision"], "rework", reviewer_member_id="dispatcher", notes="ROOT_REWORK_UNIQUE_required_final_evidence")
        provider_id = rework["execution"]["provider_session_id"]
        service.run_team_goal(goal["task_id"])
        assert service.store.provider_session(provider_id)["status"] == "completed"
        root = next(t for t in service.store.list_tasks() if t["task_id"] == goal["task_id"])
        assert root["status"] == "review"
        assert "ROOT_REWORK_UNIQUE_required_final_evidence" in json.loads(root["payload"])["provider_output"]
        assert service.review_task(root["task_id"], root["revision"], "approve", reviewer_member_id="dispatcher")["status"] == "done"
    finally:
        service.close()


def test_real_team_failure_releases_lease_and_blocks_root(tmp_path):
    (tmp_path / "providers.json").write_text(json.dumps({"providers": [{"type": "local-command", "command": [sys.executable, "-c", "import sys; sys.exit(7)"]}]}), encoding="utf-8")
    service = ButianService(tmp_path)
    try:
        service.add_member("dispatcher", "Dispatcher")
        service.add_member("worker", "Worker", provider_id="local-command", model_id="local-command")
        goal = service.submit_goal("failure", mode="team", member_id="dispatcher", member_ids=["worker"])
        result = service.run_team_goal(goal["task_id"])
        child = service.store.db.execute("SELECT * FROM tasks WHERE parent_id=?", (goal["task_id"],)).fetchone()
        assert child["status"] == "unknown"
        assert json.loads(child["payload"])["reason"] == "exit_code:7"
        assert child["lease_id"] is None
        assert result["status"] == "blocked"
        assert service.run_team_goal(goal["task_id"])["status"] == "blocked"
    finally:
        service.close()


def test_terminal_provider_can_publish_persisted_output_without_reexecution(tmp_path):
    command = [sys.executable, "-c", "import sys; sys.stdout.write('persisted-original-output')"]
    (tmp_path / "providers.json").write_text(json.dumps({"providers": [{"type": "local-command", "command": command}]}), encoding="utf-8")
    service = ButianService(tmp_path)
    try:
        service.add_member("worker", "Worker", provider_id="local-command", model_id="local-command")
        goal = service.submit_goal("resume", member_id="worker")
        pid = goal["execution"]["provider_session_id"]
        service.provider_runtime.consume(pid, service.providers.get("local-command"))
        assert service.store.provider_session(pid)["status"] == "completed"
        result = service.consume_provider_task(pid)
        assert result["task_status"] == "review"
        assert result["text"] == "persisted-original-output"
        repeated = service.consume_provider_task(pid)
        assert repeated["task_status"] == "review"
        assert repeated["text"] == "persisted-original-output"
    finally:
        service.close()


def test_team_root_rework_without_provider_does_not_reaggregate(tmp_path):
    command = [sys.executable, '-c', 'print("original-output")']
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': command}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        service.add_member('dispatcher', 'Dispatcher')
        service.add_member('worker', 'Worker', provider_id='local-command')
        goal = service.submit_goal('root rework', mode='team', member_id='dispatcher', member_ids=['worker'])
        service.run_team_goal(goal['task_id'])
        child = service.store.db.execute('SELECT * FROM tasks WHERE parent_id=?', (goal['task_id'],)).fetchone()
        service.review_task(child['task_id'], child['revision'], 'approve', reviewer_member_id='dispatcher')
        service.run_team_goal(goal['task_id'])
        root = service.store.db.execute('SELECT * FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone()
        service.review_task(root['task_id'], root['revision'], 'rework', reviewer_member_id='dispatcher', notes='new evidence required')
        result = service.run_team_goal(goal['task_id'])
        assert result['status'] == 'ready'
        root = service.store.db.execute('SELECT * FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone()
        assert root['status'] == 'ready'
    finally:
        service.close()


def test_team_question_wait_does_not_consume_previous_execution(tmp_path):
    command = [sys.executable, '-c', 'print("output-before-answer")']
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': command}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        service.add_member('dispatcher', 'Dispatcher')
        service.add_member('worker', 'Worker', provider_id='local-command')
        goal = service.submit_goal('question wait', mode='team', member_id='dispatcher', member_ids=['worker'])
        child = service.store.db.execute('SELECT * FROM tasks WHERE parent_id=?', (goal['task_id'],)).fetchone()
        qid = service.ask_user(child['task_id'], child['revision'], 'worker', 'input required')
        result = service.run_team_goal(goal['task_id'])
        assert result['children'][0]['status'] == 'waiting_user'
        assert service.store.provider_session(goal['children'][0]['execution']['provider_session_id'])['status'] == 'running'
        service.answer_user(qid, 'new-input')
        result = service.run_team_goal(goal['task_id'])
        assert result['children'][0]['status'] == 'review'
        latest = service.store.db.execute('SELECT prompt FROM provider_sessions WHERE task_id=? ORDER BY created_at DESC LIMIT 1', (child['task_id'],)).fetchone()
        assert 'new-input' in latest['prompt']
    finally:
        service.close()
