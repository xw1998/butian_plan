import json

import pytest

from butian.mcp_server import TOOLS, call_tool, list_tools_page
from butian.runtime.service import ButianService


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("BUTIAN_MCP_TOKEN_REQUIRED", raising=False)
    monkeypatch.setenv("BUTIAN_MAINTENANCE_ENABLED", "0")
    service = ButianService(tmp_path / "home")
    service.add_member("host", "Host", "分析员", ["拆解问题", "汇总结论"])
    service.add_member("analyst", "Analyst", "复核员", ["独立复核"])
    state = {"actor": None, "token": None}
    yield service, state
    service.close()


def test_connect_once_then_calls_need_no_credentials(runtime):
    service, state = runtime
    info = call_tool(service, "butian_connect", {"member_id": "host"}, state)
    assert info["connected"] is True
    assert info["token_required"] is False
    assert call_tool(service, "butian_context", {}, state)["actor_member_id"] == "host"
    assert call_tool(service, "butian_memory_search", {"member_id": "host", "query": "x"}, state)["memories"] == []
    with pytest.raises(ValueError, match="member_memory_access_denied"):
        call_tool(service, "butian_memory_search", {"member_id": "analyst", "query": "x"}, state)


def test_connect_refuses_to_guess_between_members(runtime):
    service, state = runtime
    info = call_tool(service, "butian_connect", {}, state)
    assert info["connected"] is False
    assert info["reason"] == "member_id_required"
    assert {"host", "analyst"} <= set(info["members"]) and len(info["members"]) > 1
    assert state["actor"] is None


def test_token_requirement_can_be_restored(runtime, monkeypatch):
    service, state = runtime
    monkeypatch.setenv("BUTIAN_MCP_TOKEN_REQUIRED", "1")
    with pytest.raises(ValueError, match="member_authentication_failed"):
        call_tool(service, "butian_connect", {"member_id": "host"}, state)
    assert service.local_actor is None
    assert state["actor"] is None


def test_mode_switch_needs_no_session_and_applies_to_next_goal(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    assert call_tool(service, "butian_mode", {"action": "get"}, state)["mode"] == "solo"
    switched = call_tool(service, "butian_mode", {"action": "set", "mode": "team"}, state)
    assert switched["mode"] == "team" and switched["previous_mode"] == "solo"

    goal = call_tool(service, "butian_goal_submit",
                     {"goal": "复核实验报告", "member_ids": ["analyst"],
                      "problem": {"acceptance_criteria": ["给出结论与证据"]}}, state)
    assert goal["session"]["mode"] == "team"
    assert goal["plan_source"] == "deterministic_role_split"
    assert [child["member_id"] for child in goal["children"]] == ["analyst"]
    assert "职责：" in goal["children"][0]["instruction"]

    back = call_tool(service, "butian_mode", {"action": "set", "mode": "solo"}, state)
    assert back["session"] and back["session"]["changed"] is True
    solo = call_tool(service, "butian_goal_submit",
                     {"goal": "写一份说明", "problem": {"acceptance_criteria": ["说明完整"]}}, state)
    assert solo["session"]["mode"] == "solo"


def test_explicit_subtasks_still_win(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    goal = call_tool(service, "butian_goal_submit",
                     {"goal": "复核实验", "mode": "team", "member_ids": ["analyst"],
                      "subtasks": ["复核统计口径与样本量"],
                      "problem": {"acceptance_criteria": ["指出口径问题"]}}, state)
    assert goal["plan_source"] == "host"
    assert goal["children"][0]["instruction"] == "复核统计口径与样本量"


def test_context_hides_other_members_tasks(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    session = service.create_session("solo", "analyst")
    hidden = service.store.create_task(session["session_id"], "analyst 私有任务", assignee_id="analyst", payload={"mode": "solo"})
    own_session = service.create_session("solo", "host")
    mine = service.store.create_task(own_session["session_id"], "host 任务", assignee_id="host", payload={"mode": "solo"})
    context = call_tool(service, "butian_context", {}, state)
    ids = {task["task_id"] for task in context["tasks"]}
    assert mine in ids and hidden not in ids
    assert context["mode"] == "solo"
    assert {"host", "analyst"} <= {member["member_id"] for member in context["members"]}


def test_status_reports_identity_and_mode(runtime):
    service, state = runtime
    before = call_tool(service, "butian_status", {}, state)
    assert before["next_tool"] == "butian_connect" and before["actor_member_id"] is None
    call_tool(service, "butian_connect", {"member_id": "analyst"}, state)
    after = call_tool(service, "butian_status", {}, state)
    assert after["actor_member_id"] == "analyst" and after["mode"] == "solo"
    assert after["next_tool"] == "butian_context"


def test_first_page_is_a_starter_kit_without_duplicates():
    names = []
    cursor = None
    while True:
        page = list_tools_page(cursor)
        names.extend(tool["name"] for tool in page["tools"])
        cursor = page.get("nextCursor")
        if cursor is None:
            break
    assert len(names) == len(set(names)) == len(TOOLS)
    assert names[:4] == ["butian_status", "butian_connect", "butian_context", "butian_mode"]
    assert "butian_goal_submit" in names[:8]
