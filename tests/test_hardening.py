import pytest

from butian.runtime.service import ButianService
from butian.mcp_server import call_tool, TOOLS


def test_evidence_becomes_stale(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("one", encoding="utf-8")
    service = ButianService(tmp_path / "home")
    evidence_id = service.store.record_evidence(source)
    assert service.store.verify_evidence(evidence_id)["status"] == "valid"
    source.write_text("two", encoding="utf-8")
    assert service.store.verify_evidence(evidence_id)["status"] == "stale"
    service.close()


def test_mcp_tools_cover_minimum_runtime(tmp_path):
    service = ButianService(tmp_path)
    assert {"name": "butian_task_create", "description": "Create a task", "inputSchema": {"type": "object", "required": ["session_id", "title"], "properties": {"session_id": {"type": "string"}, "title": {"type": "string"}, "assignee_id": {"type": "string"}}}} in TOOLS
    session = call_tool(service, "butian_session_create", {"mode": "solo"})
    task = call_tool(service, "butian_task_create", {"session_id": session["session_id"], "title": "验证"})
    assert task["task_id"].startswith("task_")
    call_tool(service, "butian_task_transition", {"task_id": task["task_id"], "expected_revision": 1, "target": "ready", "operation_key": "mcp-1"})
    assert call_tool(service, "butian_task_list", {"status": "ready"})["tasks"][0]["status"] == "ready"
    service.close()


def test_transition_idempotency_does_not_reapply_revision(tmp_path):
    from butian.runtime.store import Store
    store = Store(tmp_path / "team.db")
    store.add_member("d", "派活者")
    session = store.create_session("team", "d", None, {})
    task = store.create_task(session, "idempotent")
    store.transition_task(task, 1, "ready", "same-op")
    store.transition_task(task, 1, "ready", "same-op")
    row = store.list_tasks()[0]
    assert row["status"] == "ready" and row["revision"] == 2
    assert store.db.execute("SELECT COUNT(*) FROM events WHERE idempotency_key='same-op'").fetchone()[0] == 1
    store.close()


def test_memory_source_event_must_exist(tmp_path):
    from butian.runtime.memory import MemoryStore
    memory = MemoryStore(tmp_path / "memory.db")
    with pytest.raises(ValueError, match="invalid_memory_source_events"):
        memory.put("m1", "lesson", "不可伪造", source_event_ids=["fake"], source_validator=lambda ids: False)
    memory.close()
