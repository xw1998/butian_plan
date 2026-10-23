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
    # host 没配 Provider：合法下一步是“导出上下文 → 本机做完 → 交回证据”，
    # 不是先迁 ready 再认领（那条路要求 Provider 真能派活，这里派不出去）。
    assert first["tool"] == "butian_execution_export"
    assert first["arguments"] == {"task_id": goal["task_id"], "actor_member_id": "host"}
    assert first["then"]["tool"] == "butian_execution_submit"
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
    # 派发者身份不代做别人的任务；没有 Provider 时给的是“以成员身份导出执行”，
    # 产出仍记在该成员名下，独立执行与独立审核都还在。
    external = [a for a in ctx["next_actions"] if a.get("tool") == "butian_execution_export"]
    assert external and external[0]["as_member"] == "analyst"
    assert external[0]["arguments"] == {"task_id": child, "actor_member_id": "analyst"}
    assert any(t["task_id"] == child for t in ctx["tasks"])

    call_tool(service, "butian_connect", {"member_id": "analyst"}, state)
    own = call_tool(service, "butian_context", {}, state)
    assert own["next_actions"][0]["tool"] == "butian_execution_export"
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

def test_team_mode_without_provider_is_not_a_dead_end(runtime):
    """没有 Provider 的团队模式：子任务要能由宿主以成员身份执行完，不能空转。

    这条守的是“打不开的钥匙”：父任务导出会被 team_children_not_reviewed 拒，
    所以父任务不能给导出动作；子任务给的导出动作必须带负责人本人的身份，
    而且照着做完之后团队真的能走到待审核。
    """
    service, state = runtime
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    call_tool(service, "butian_mode", {"action": "set", "mode": "team"}, state)
    plan = call_tool(service, "butian_goal_submit",
                     {"goal": "把导出流程写成脚本", "member_ids": ["analyst"],
                      "subtasks": ["写脚本"],
                      "problem": {"acceptance_criteria": ["脚本能跑"]}}, state)
    root = plan["task_id"]
    child = plan["children"][0]["task_id"]

    ran = call_tool(service, "butian_task_team_run", {"task_id": root}, state)
    assert ran["children"][0]["status"] == "awaiting_external_execution"
    actions = ran["children"][0]["next_actions"]
    assert actions[0]["tool"] == "butian_execution_export"
    assert actions[0]["as_member"] == "analyst"
    assert actions[0]["then"]["tool"] == "butian_execution_submit"

    ctx = call_tool(service, "butian_context", {}, state)
    exported_roots = [action for action in ctx["next_actions"]
                      if action.get("tool") == "butian_execution_export"
                      and action["arguments"]["task_id"] == root]
    assert not exported_roots, "父任务此刻导出必被拒，不能把它当成下一步"
    assert any(action.get("tool") == "butian_execution_export"
               and action["arguments"]["task_id"] == child for action in ctx["next_actions"])

    # 指引要求的顺序：先切到该成员身份，再导出执行（成员各有独立记忆与身份）。
    assert actions[0]["before"] == {"tool": "butian_connect", "arguments": {"member_id": "analyst"}}
    call_tool(service, "butian_connect", {"member_id": "analyst"}, state)
    package = call_tool(service, "butian_execution_export",
                        {"task_id": child, "actor_member_id": "analyst"}, state)
    proof = service.home / "proofs" / "team-child-1.txt"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text("脚本已写成并跑通", encoding="utf-8")
    evidence_id = service.record_evidence(proof, owner_member_id="analyst", actor_member_id="analyst")
    delivered = call_tool(service, "butian_execution_submit",
                          {"task_id": child, "expected_revision": package["task_revision"],
                           "problem_revision": package["problem_revision"],
                           "submission_id": "sub-team-child-1", "output": "脚本已写成并跑通",
                           "evidence_ids": [evidence_id], "actor_member_id": "analyst"}, state)
    assert delivered["status"] == "review"

    # 交付完切回派发者身份：汇总与审核都不能由被审人自己来。
    call_tool(service, "butian_connect", {"member_id": "host"}, state)
    after = call_tool(service, "butian_task_team_run", {"task_id": root}, state)
    assert after["status"] == "awaiting_child_reviews"
