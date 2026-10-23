"""易用性回归（AI 自测第二轮）：身份缺省、null 参数、空 next_actions、源快照闭环、任务树 payload。"""

import hashlib

import pytest

from butian.mcp_server import TOOLS, call_tool
from butian.runtime.mcp_guidance import ToolError, recovery_details
from butian.runtime.service import ButianService

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("BUTIAN_MCP_TOKEN_REQUIRED", raising=False)
    monkeypatch.setenv("BUTIAN_MAINTENANCE_ENABLED", "0")
    service = ButianService(tmp_path / "home")
    service.add_member("host", "Host", "分析员", ["拆解问题"])
    service.add_member("analyst", "Analyst", "复核员", ["独立复核"])
    state = {"actor": None, "token": None}
    yield service, state
    service.close()

def _tool(name):
    return next(tool for tool in TOOLS if tool["name"] == name)

def test_member_id_defaults_to_connected_member_for_own_member_tools():
    # 对外契约仍声明 member_id（这些工具确实要认证），但连上之后不必每次重传。
    for name in ("butian_memory_search", "butian_tool_list", "butian_evolution_degradation",
                 "butian_tool_executions", "butian_hypothesis_action_start"):
        assert "member_id" in (_tool(name)["inputSchema"].get("required") or []), name
        assert "defaults to the connected member" in _tool(name)["inputSchema"]["properties"]["member_id"]["description"]

def test_member_id_stays_explicit_where_it_means_someone_else():
    # grant 的 member_id 是被授权的假设所有者；默认成自己会把独立授权变成自我授权。
    for name in ("butian_hypothesis_action_grant", "butian_tool_security_review",
                 "butian_tool_repair_confirm", "butian_dispatcher_set"):
        assert "member_id" in (_tool(name)["inputSchema"].get("required") or []), name

def test_identity_must_be_declared_until_connected(runtime):
    # 没连身份时缺 member_id 要报错，但错误必须直接指向 butian_connect，而不是让 AI 猜。
    service, state = runtime
    with pytest.raises(ToolError) as err:
        call_tool(service, "butian_memory_search", {}, state)
    assert "butian_connect" in [action.get("tool") for action in err.value.details["next_actions"]]

def test_member_id_is_injected_from_connection(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    tools = call_tool(service, "butian_tool_list", {}, state)
    assert tools["tools"] == [] or isinstance(tools["tools"], list)
    executions = call_tool(service, "butian_tool_executions", {}, state)
    assert isinstance(executions.get("executions"), list)

def test_goal_submit_honors_top_level_acceptance_criteria(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    result = call_tool(service, "butian_goal_submit",
                       {"goal": "核验标定流程", "acceptance_criteria": ["给出步骤与实测证据"]}, state)
    assert result["problem"]["definition_status"] == "defined"
    assert result["problem"]["acceptance_criteria"] == ["给出步骤与实测证据"]
    assert "clarification" not in result

def test_goal_submit_rejects_unknown_argument_instead_of_ignoring_it(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    with pytest.raises(ToolError) as err:
        call_tool(service, "butian_goal_submit",
                  {"goal": "核验标定流程", "acceptance_criterai": ["拼错的字段名"]}, state)
    assert err.value.details["code"] == "unknown_argument"
    assert err.value.details["unexpected"] == ["acceptance_criterai"]
    assert "acceptance_criteria" in err.value.details["accepted"]
    assert service.store.list_tasks() == []

def test_goal_submit_merges_duplicated_problem_field_instead_of_failing(runtime):
    """同一字段顶层与 problem 里都写不是错误：列表取并集，信息不丢，也不再把合法调用判死。"""
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    result = call_tool(service, "butian_goal_submit",
                       {"goal": "核验标定流程", "acceptance_criteria": ["顶层写法"],
                        "problem": {"acceptance_criteria": ["嵌套写法"]}}, state)
    assert result["problem"]["acceptance_criteria"] == ["嵌套写法", "顶层写法"]
    assert result["problem_fields_merged"] == ["acceptance_criteria"]
    assert result["problem"]["definition_status"] == "defined"

def test_null_optional_argument_is_treated_as_absent(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    result = call_tool(service, "butian_goal_submit",
                       {"goal": "核验标定流程", "acceptance_criteria": ["给出步骤与实测证据"],
                        "member_ids": None, "task_id": None}, state)
    assert result.get("task_id")

def test_recovery_details_never_returns_empty_next_actions():
    cases = [
        ("butian_knowledge_source_read", {"source_id": "src_missing"}, "knowledge_source_not_found"),
        ("butian_evolution_degradation", {"member_id": "host", "memory_id": "mem_missing"}, "memory_not_found"),
        ("butian_task_team_run", {"actor_member_id": "host", "task_id": "task_missing"}, "team_session_required"),
        ("butian_context_recover", {"actor_member_id": "host", "task_id": "task_missing"}, "history_recovery_selector_required"),
        ("butian_unknown_thing", {"actor_member_id": "host"}, "some_unclassified_error"),
    ]
    for tool, args, code in cases:
        details = recovery_details(None, tool, args, code)
        assert details["next_actions"], code
        assert all(action.get("tool") or action.get("action") for action in details["next_actions"]), code

def test_knowledge_source_verify_makes_source_readable(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    content = "雨量计每年标定一次；标定后需保存原始读数。"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    source = call_tool(service, "butian_knowledge_source_add",
                       {"source_type": "manual", "locator": "local://calib.md",
                        "title": "标定手册", "content_hash": digest}, state)
    source_id = source["source_id"]

    # 只登记哈希时没有正文可读：错误必须指向 verify，而不是回空 next_actions。
    with pytest.raises(ToolError) as missing:
        call_tool(service, "butian_knowledge_source_read", {"source_id": source_id}, state)
    assert missing.value.details["code"] == "source_snapshot_not_found"
    assert any(action.get("tool") == "butian_knowledge_source_verify"
               for action in missing.value.details["next_actions"])

    verified = call_tool(service, "butian_knowledge_source_verify",
                         {"source_id": source_id, "content": content}, state)
    assert verified["status"] == "verified"
    read = call_tool(service, "butian_knowledge_source_read", {"source_id": source_id}, state)
    assert read["content"] == content
    assert read["sha256"] == digest

def test_handoff_status_keeps_accepting_legacy_values(runtime, tmp_path):
    # 既有调用方写 status="completed"、且不传 confidence：两者都不该在 schema 层被拦。
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    note = tmp_path / "calib-note.txt"
    note.write_text("标定流程步骤与实测记录", encoding="utf-8")
    evidence_id = service.record_evidence(str(note), owner_member_id="host", actor_member_id="host")
    goal = call_tool(service, "butian_goal_submit",
                     {"goal": "核验标定流程", "acceptance_criteria": ["给出步骤与实测证据"]}, state)
    task_id = goal["task_id"]
    tasks = service.store.list_tasks()
    revision = next(task["revision"] for task in tasks if task["task_id"] == task_id)
    call_tool(service, "butian_task_transition",
              {"task_id": task_id, "expected_revision": revision, "target": "ready"}, state)
    revision += 1
    call_tool(service, "butian_task_claim", {"task_id": task_id}, state)
    revision += 1
    call_tool(service, "butian_task_transition",
              {"task_id": task_id, "expected_revision": revision, "target": "running"}, state)
    revision += 1
    call_tool(service, "butian_task_transition",
              {"task_id": task_id, "expected_revision": revision, "target": "review"}, state)
    revision += 1
    handoff = call_tool(service, "butian_task_handoff",
                        {"task_id": task_id, "task_revision": revision, "status": "completed",
                         "summary": "已完成标定流程核验", "evidence_ids": [evidence_id], "next_action": "审核"},
                        state)
    assert handoff["handoff_revision"] == revision
    assert handoff["review_ready"] is True
    assert handoff["confidence"] == 0.5
    assert handoff["confidence_defaulted"] is True

def test_task_tree_returns_parsed_payload(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    call_tool(service, "butian_goal_submit",
              {"goal": "核验标定流程", "acceptance_criteria": ["给出步骤与实测证据"]}, state)
    tree = call_tool(service, "butian_task_tree", {}, state)["tree"]
    assert tree and isinstance(tree[0]["payload"], dict)
    assert tree[0]["payload"]["goal"] == "核验标定流程"
