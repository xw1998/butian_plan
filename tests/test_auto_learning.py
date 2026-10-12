import json

from butian.mcp_server import TOOLS, call_tool
from butian.runtime.service import ButianService


def test_practice_outcome_candidate_then_warm(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m", "Member")
        first = service.record_practice_outcome("m", memory_id="lesson-1", kind="lesson", content="keep evidence", outcome="success", independent_key="run-a", evidence_ref="evidence-a")
        assert first["status"] == "candidate"
        second = service.record_practice_outcome("m", memory_id="lesson-1", kind="lesson", content="keep evidence", outcome="success", independent_key="run-b", evidence_ref="evidence-b")
        assert second["status"] == "warm"
        assert second["successes"] == 2
    finally:
        service.close()


def test_practice_outcome_rejects_unsupported_evidence_and_duplicate(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m", "Member")
        try:
            service.record_practice_outcome("m", memory_id="lesson-2", kind="lesson", content="x", outcome="success", independent_key="run-a")
        except ValueError as exc:
            assert str(exc) == "practice_success_evidence_required"
        else:
            raise AssertionError("missing evidence was accepted")
        service.record_practice_outcome("m", memory_id="lesson-2", kind="lesson", content="x", outcome="success", independent_key="run-a", evidence_ref="e")
        try:
            service.record_practice_outcome("m", memory_id="lesson-2", kind="lesson", content="x", outcome="success", independent_key="run-a", evidence_ref="e2")
        except ValueError as exc:
            assert str(exc) == "duplicate_independent_observation"
        else:
            raise AssertionError("duplicate observation was accepted")
    finally:
        service.close()


def test_mcp_practice_tool_is_registered_and_callable(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m", "Member")
        service.store.set_member_token("m", "secret")
        assert any(tool["name"] == "butian_memory_practice" for tool in TOOLS)
        json.dumps(TOOLS)
        result = call_tool(service, "butian_memory_practice", {"member_id": "m", "memory_id": "lesson-3", "kind": "lesson", "content": "x", "outcome": "failure", "independent_key": "run-a", "actor_member_id": "m", "token": "secret"})
        assert result["status"] == "candidate"
    finally:
        service.close()


def test_review_creates_audited_candidate_once(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("worker", "执行者")
        service.add_member("reviewer", "审核者")
        session = service.create_session("team", "reviewer")
        task_id = service.store.create_task(session["session_id"], "reviewed task", "worker")
        for revision, state in ((1, "ready"), (2, "claimed"), (3, "running"), (4, "review")):
            service.store.transition_task(task_id, revision, state, "review-test-" + state)
        evidence_path = tmp_path / "review-evidence.txt"
        evidence_path.write_text("verified", encoding="utf-8")
        evidence_id = service.record_evidence(evidence_path, owner_member_id="worker", actor_member_id="worker")
        service.submit_handoff({"task_id": task_id, "task_revision": 5, "member_id": "worker", "status": "done", "summary": "完成实现", "evidence_ids": [evidence_id], "next_action": "交付", "confidence": 0.9})
        result = service.review_task(task_id, 5, "approve", reviewer_member_id="reviewer")
        learning = result["learning"]
        assert learning["candidate"] is True
        memory = service.memory("worker")
        try:
            assert memory.get(learning["memory_id"])["status"] == "candidate"
        finally:
            memory.close()
        assert service.store.validate_source_events([learning["event_id"]], member_id="worker")
        handoff = service.store.db.execute("SELECT payload FROM handoffs WHERE task_id=? AND revision=?", (task_id, 5)).fetchone()
        task = {"task_id": task_id, "payload": "{}"}
        duplicate = service._record_review_learning(task, 5, "approve", "", handoff)
        assert duplicate["status"] == "committed"
        assert service.store.db.execute("SELECT COUNT(*) FROM member_memory_outbox WHERE memory_id=?", (learning["memory_id"],)).fetchone()[0] == 1
    finally:
        service.close()


def test_real_task_candidate_reuse_counterevidence_and_restart(tmp_path):
    import sys
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': [sys.executable, '-c', 'import sys; text=sys.stdin.read(); print("calibration evidence verified")'] }]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker', provider_id='local-command')
        goal = service.submit_goal('calibration', member_id='worker')
        result = service.consume_provider_task(goal['execution']['provider_session_id'])
        assert result['status'] == 'completed'
        task = service.store.db.execute('SELECT * FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone()
        learning = service.review_task(task['task_id'], task['revision'], 'approve', reviewer_member_id='worker')['learning']
        mid = learning['memory_id']
        memory = service.memory('worker')
        try:
            item = memory.get(mid)
            assert item['status'] == 'candidate'
            content = item['content']
        finally:
            memory.close()
        composed, report = service.compose_context('worker', 'calibration')
        assert mid in report.recalled_memory_ids
        assert 'candidate' in composed
        for key in ('independent-a', 'independent-b'):
            service.record_practice_outcome('worker', memory_id=mid, kind='task_review_candidate', content=content, outcome='success', independent_key=key, evidence_ref=learning['event_id'])
        memory = service.memory('worker')
        try:
            assert memory.get(mid)['status'] == 'warm'
            memory.record_usage_feedback(mid, outcome='harmful', evidence_ref='counterexample-real-fixture')
            assert memory.get(mid)['status'] == 'disputed'
            memory.put(mid, 'task_review_candidate', content + ' revised preconditions', status='candidate', change_reason='counterevidence_revision', source_event_ids=[learning['event_id']])
        finally:
            memory.close()
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        chain = service.memory_versions('worker', mid)
        assert chain['chain']['valid']
        assert len(chain['versions']) >= 2
        composed, report = service.compose_context('worker', 'calibration')
        assert mid in report.recalled_memory_ids
        assert 'revised preconditions' in composed
        assert service.store.db.execute('SELECT status FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone()[0] == 'done'
    finally:
        service.close()
