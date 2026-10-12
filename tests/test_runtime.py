from butian.runtime.service import ButianService


def test_member_memory_and_model_negotiation(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("m1", "研究员", model_id="special")
    memory = service.memory("m1")
    memory.put("mem1", "lesson", "先验证证据", confidence=0.9, source_event_ids=["evt_test"], status="warm")
    assert memory.search("证据")[0]["memory_id"] == "mem1"
    memory.close()
    session = service.create_session("team", "m1", "special", {"can_select_model": False})
    result = service.select_model(session["session_id"], "special", {"can_select_model": False})
    assert result["selection_status"] == "unsupported"
    service.close()


def test_question_and_handoff_require_structure(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("m1", "派活者")
    session = service.create_session("team", "m1")
    task = service.store.create_task(session["session_id"], "确认需求", "m1")
    service.store.transition_task(task, 1, "ready", "ready-1")
    service.store.transition_task(task, 2, "claimed", "claim-1")
    service.store.transition_task(task, 3, "running", "run-1")
    question_id = service.ask_user(task, 4, "m1", "请选择范围")
    assert service.store.db.execute("select status from tasks where task_id=?", (task,)).fetchone()[0] == "waiting_user"
    assert service.answer_user(question_id, "全部") == task
    # 新校验：旧 revision 交接被拒
    try:
        service.submit_handoff({"task_id": task, "task_revision": 5, "member_id": "m1", "status": "done", "summary": "旧交接", "evidence_ids": [], "next_action": "review", "confidence": 0.8})
        raise AssertionError("stale handoff should be rejected")
    except ValueError as exc:
        assert "stale" in str(exc)
    # 空证据交接可以入账（供审核参考），但不能让任务进入 done
    service.submit_handoff({"task_id": task, "task_revision": 6, "member_id": "m1", "status": "done", "summary": "已确认", "evidence_ids": [], "next_action": "review", "confidence": 0.8})
    service.close()


def test_user_answer_preserves_goal_instruction_and_environment(tmp_path):
    import json
    service = ButianService(tmp_path)
    try:
        service.add_member("m1", "Worker")
        session = service.create_session("solo", current_member_id="m1")
        original = {"goal": "goal", "instruction": "instruction", "environment": {"dataset": "v1"}}
        task_id = service.store.create_task(session["session_id"], "question", "m1", payload=original)
        for revision, status in enumerate(["ready", "claimed", "running"], 1):
            service.store.transition_task(task_id, revision, status, f"question-state-{revision}")
        question_id = service.ask_user(task_id, 4, "m1", "scope?")
        service.answer_user(question_id, "all")
        row = service.store.db.execute("SELECT status,payload FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        payload = json.loads(row["payload"])
        assert all(payload.get(key) == value for key, value in original.items())
        assert payload["answer"] == "all"
        assert row["status"] == "ready"
    finally:
        service.close()


def test_rejected_question_does_not_leave_open_question(tmp_path):
    import pytest
    service = ButianService(tmp_path)
    service.add_member("owner", "Owner")
    session = service.create_session("solo", current_member_id="owner")
    task_id = service.store.create_task(session["session_id"], "scope", "owner")
    try:
        with pytest.raises(ValueError):
            service.ask_user(task_id, 99, "owner", "stale question")
        assert service.store.db.execute("SELECT COUNT(*) FROM questions WHERE task_id=?", (task_id,)).fetchone()[0] == 0
        assert service.store.db.execute("SELECT revision,status FROM tasks WHERE task_id=?", (task_id,)).fetchone()["status"] == "todo"
    finally:
        service.close()
