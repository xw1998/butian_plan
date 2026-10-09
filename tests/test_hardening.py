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
