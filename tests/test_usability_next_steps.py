"""易用性修复回归：下一步动作按状态给、静默参数改显式、schema 与实际鉴权一致。"""

import pytest

from butian.mcp_server import TOOLS, call_tool, list_tools_page
from butian.runtime.mcp_guidance import ToolError
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


def _task(service, task_id):
    return next(task for task in service.store.list_tasks() if task["task_id"] == task_id)


def _tool(name):
    return next(tool for tool in TOOLS if tool["name"] == name)


def test_solo_goal_rejects_member_ids_instead_of_dropping_them(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    with pytest.raises(ToolError) as err:
        call_tool(service, "butian_goal_submit",
                  {"goal": "复核数据", "member_ids": ["analyst"]}, state)
    assert err.value.details["code"] == "member_ids_require_team_mode"
    assert err.value.details["mode"] == "solo"
    assert err.value.details["member_ids"] == ["analyst"]
    assert any(a.get("tool") == "butian_mode" for a in err.value.details["next_actions"])
    # 报错就是没建任务，不能留下半成品
    assert service.store.list_tasks() == []


def test_catalog_query_filters_and_reports_no_match(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    hit = call_tool(service, "butian_tools_catalog", {"query": "task_claim"}, state)
    assert [t["name"] for t in hit["tools"]] == ["butian_task_claim"]
    with pytest.raises(ToolError) as err:
        call_tool(service, "butian_tools_catalog", {"query": "no_such_tool_xyz"}, state)
    assert err.value.details["code"] == "catalog_query_no_match"


def test_task_claim_omits_member_id_and_fits_local_trust(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    goal = call_tool(service, "butian_goal_submit",
                     {"goal": "单人任务", "problem": {"acceptance_criteria": ["给出结论"]}}, state)
    task_id = goal["task_id"]
    call_tool(service, "butian_task_transition",
              {"task_id": task_id, "expected_revision": 1, "target": "ready"}, state)
    claim = call_tool(service, "butian_task_claim", {"task_id": task_id}, state)
    assert claim["lease_id"].startswith("lease_")
    assert _task(service, task_id)["assignee_id"] == "host"


def test_transition_derives_operation_key_when_omitted(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    goal = call_tool(service, "butian_goal_submit",
                     {"goal": "单人任务", "problem": {"acceptance_criteria": ["给出结论"]}}, state)
    task_id = goal["task_id"]
    result = call_tool(service, "butian_task_transition",
                       {"task_id": task_id, "expected_revision": 1, "target": "ready"}, state)
    assert result["operation_key"] == f"{task_id}:1:ready"
    assert _task(service, task_id)["status"] == "ready"


def test_context_offers_advancing_step_before_new_goal(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    goal = call_tool(service, "butian_goal_submit",
                     {"goal": "单人任务", "problem": {"acceptance_criteria": ["给出结论"]}}, state)
    ctx = call_tool(service, "butian_context", {}, state)
    first = ctx["next_actions"][0]
    assert first["tool"] == "butian_task_transition"
    assert first["arguments"] == {"task_id": goal["task_id"], "expected_revision": 1,
                                  "target": "ready", "operation_key": f"{goal['task_id']}:1:ready"}
    # 有可推进任务时不再兜售新建目标
    assert all(a.get("tool") != "butian_goal_submit" for a in ctx["next_actions"])


def test_dispatcher_is_not_told_to_do_someone_elses_task(runtime):
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    call_tool(service, "butian_mode", {"action": "set", "mode": "team"}, state)
    plan = call_tool(service, "butian_goal_submit",
                     {"goal": "复核数据", "member_ids": ["analyst"],
                      "problem": {"acceptance_criteria": ["给出独立复核意见"]}}, state)
    child = plan["children"][0]["task_id"]
    ctx = call_tool(service, "butian_context", {}, state)
    advancing = [a for a in ctx["next_actions"] if a.get("tool") == "butian_task_transition"]
    assert all(a["arguments"]["task_id"] != child for a in advancing)
    # 派发者自己的父任务仍可推进，所以这里只要求"不代做别人那条"，
    # 不要求立刻出现等待提示（父任务推完后才会出现）。
    assert any(t["task_id"] == child for t in ctx["tasks"])

    call_tool(service, "butian_connect", {"member_id": "analyst"}, state)
    own = call_tool(service, "butian_context", {}, state)
    assert own["next_actions"][0]["tool"] == "butian_task_transition"
    assert own["next_actions"][0]["arguments"]["task_id"] == child


def test_schema_does_not_demand_credentials_it_derives(runtime):
    for name in ("butian_task_claim", "butian_task_transition", "butian_task_list"):
        required = _tool(name)["inputSchema"]["required"]
        assert "token" not in required and "actor_member_id" not in required
    assert _tool("butian_task_claim")["inputSchema"]["required"] == ["task_id"]
    assert _tool("butian_collaboration_start")["inputSchema"]["required"] == ["goal"]
    assert "required" not in _tool("butian_collaboration_preflight")["inputSchema"]


def test_unconfirmed_execution_is_marked_untrustworthy_with_retry_step():
    """执行没有结论时不能只回一个 unknown：要说清不采信、为什么、下一步怎么重发。"""
    from butian.runtime.usability import describe_provider_failure

    result = describe_provider_failure({'status': 'unknown', 'reason': 'provider_executor_unconfirmed'},
                                       task_id='task_1', member_id='analyst')
    assert result['trustworthy'] is False
    assert result['diagnosis']['reason'] == 'provider_executor_unconfirmed'
    assert '不采信' in result['diagnosis']['meaning'] or '当完成' in result['diagnosis']['meaning']
    tools = [action['tool'] for action in result['next_actions']]
    assert tools == ['butian_context', 'butian_provider_start']
    assert result['next_actions'][1]['arguments'] == {'task_id': 'task_1', 'member_id': 'analyst'}
    # 没有任务号的会话（例如规划期）只给上下文，不给一个填不出参数的调用。
    orphan = describe_provider_failure({'status': 'unknown'}, task_id=None)
    assert [action['tool'] for action in orphan['next_actions']] == ['butian_context']
    assert orphan['diagnosis']['reason'] == 'provider_status_unknown'

def test_first_page_puts_task_loop_before_execution_preflight():
    names = [tool["name"] for tool in list_tools_page()["tools"]]
    assert "butian_task_list" in names
    assert "butian_collaboration_preflight" not in names
