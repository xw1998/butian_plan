"""单人／团队目标提交的易用性契约。

三件事必须同时成立：
1. 本机免令牌模式下，走 call_tool 的 host 提交（网页单人目标就是这条路）不能因为“没口令”失败；
2. 团队目标由“成员 → 子任务”直接配对，不靠两份列表的条数对齐；
3. 同一执行者在同一模式下重复提交同名目标要幂等复用已有任务；团队与单人分别去重，
   单人同名目标不能被团队根任务吸收；刻意并行时改一句描述即可。
"""
import json
import threading
from pathlib import Path
from urllib import request as urlrequest

import pytest

from butian.mcp_server import call_tool
from butian.runtime.service import ButianService

WEB = Path(__file__).resolve().parents[1] / "butian" / "web"


def _service(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("host", "派活者", role="host")
    service.add_member("m1", "张三")
    service.add_member("m2", "李四")
    return service


def test_local_mode_goal_submit_needs_no_token(tmp_path):
    """本机绑定 loopback 时，网页的“单人目标”不要求口令（此前会报 invalid_tool_arguments/token_required）。"""
    from butian.runtime import http_api

    service = _service(tmp_path)
    http_api.ApiHandler.service = service
    http_api.ApiHandler.experiments = None
    http_api.ApiHandler.web_root = WEB
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        body = json.dumps({"goal": "本机提交一个目标", "mode": "solo",
                           "member_id": "host", "dispatcher_member_id": "host",
                           "acceptance_criteria": ["能被审核通过"]}).encode()
        req = urlrequest.Request(base + "/api/goals/host-submit", data=body,
                                 headers={"Content-Type": "application/json", "X-Butian-Member": "host"},
                                 method="POST")
        with urlrequest.urlopen(req) as resp:
            payload = json.loads(resp.read())
        assert payload["status"] == "ok", payload
        assert payload["data"]["task_id"].startswith("task_")
        assert payload["data"]["status"] == "awaiting_host"
    finally:
        server.shutdown()
        server.server_close()
        service.close()


def test_team_goal_pairs_each_member_with_its_own_subtask(tmp_path):
    """团队分工按“成员→子任务”配对，不存在“行数必须等于成员数”的耦合。"""
    service = _service(tmp_path)
    try:
        service.enable_local_actor("host")
        result = call_tool(service, "butian_goal_submit", {
            "actor_member_id": "host", "token": "",
            "goal": "把一份方案评审完", "mode": "team",
            "member_ids": ["m1", "m2"],
            "subtasks": ["整理输入数据", "复核边界条件"],
            "acceptance_criteria": ["两个子任务都有交付"],
        })
    finally:
        service.close()
    assert result["status"] in {"dispatched", "awaiting_host"}, result
    children = result.get("children") or []
    assert [(c["member_id"], c["instruction"]) for c in children] == [
        ("m1", "整理输入数据"), ("m2", "复核边界条件")]


def test_duplicate_goal_is_idempotent_and_returns_the_same_task(tmp_path):
    """同名目标重复提交返回已有任务：以前只加一句提示、任务照建，于是同一目标两条 todo。"""
    service = _service(tmp_path)
    try:
        service.enable_local_actor("host")
        first = call_tool(service, "butian_goal_submit", {
            "actor_member_id": "host", "token": "",
            "goal": "同一句话的目标", "acceptance_criteria": ["完成"]})
        second = call_tool(service, "butian_goal_submit", {
            "actor_member_id": "host", "token": "",
            "goal": "同一句话的目标", "acceptance_criteria": ["完成"]})
    finally:
        service.close()
    assert "duplicate_of" not in first
    assert first["created"] is True
    assert second["task_id"] == first["task_id"]
    assert second["created"] is False and second["reused_existing"] is True
    assert second["duplicate_of"]["task_id"] == first["task_id"]
    assert second["next_actions"], second


def test_team_root_does_not_swallow_a_solo_goal_of_the_same_wording(tmp_path):
    """去重必须按模式分开：团队根任务挂在派活者名下，若单人同名目标被它吸走，
    用户“我一个人先做”的意图就被静默改写成团队任务；反过来也一样。"""
    service = _service(tmp_path)
    try:
        service.enable_local_actor("host")
        team = call_tool(service, "butian_goal_submit", {
            "actor_member_id": "host", "token": "", "mode": "team",
            "goal": "同一句话的目标", "acceptance_criteria": ["完成"],
            "member_ids": ["host"], "subtasks": ["自己先看一遍"]})
        solo = call_tool(service, "butian_goal_submit", {
            "actor_member_id": "host", "token": "", "mode": "solo",
            "goal": "同一句话的目标", "acceptance_criteria": ["完成"]})
        solo_again = call_tool(service, "butian_goal_submit", {
            "actor_member_id": "host", "token": "", "mode": "solo",
            "goal": "同一句话的目标", "acceptance_criteria": ["完成"]})
        roots = [dict(row) for row in service.store.db.execute(
            "SELECT task_id, payload FROM tasks WHERE parent_id IS NULL ORDER BY rowid")]
    finally:
        service.close()
    assert solo["created"] is True, solo
    assert solo["task_id"] != team["task_id"]
    assert solo_again["task_id"] == solo["task_id"]
    assert solo_again["reused_existing"] is True
    assert len(roots) == 2, roots
    assert [json.loads(row["payload"])["mode"] for row in roots] == ["team", "solo"]


def test_web_goal_form_switches_mode_without_field_count_coupling():
    source = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'data-goal-mode="solo"' in source and 'data-goal-mode="team"' in source
    # 每个成员自带一个子任务输入框：配错数量在结构上不可能发生
    assert "data-member-task=" in source and "data-member-pick" in source
    assert "执行成员数量必须与子任务行数一致" not in source
    assert 'data-form="team-goal"' not in source and 'data-form="solo-goal"' not in source
    assert "captureGoalDraft" in source


@pytest.mark.parametrize("tool", ["butian_goal_submit"])
def test_goal_submit_identity_errors_are_semantic(tool):
    """缺身份时要报 actor_member_id_required 之类，而不是让人去翻 schema 的 invalid_tool_arguments。"""
    from butian.mcp_server import TOOLS
    from butian.runtime.mcp_guidance import ToolError, validate_arguments
    with pytest.raises(ToolError) as excinfo:
        validate_arguments(tool, {"goal": "x"}, TOOLS)
    assert str(excinfo.value) in {"actor_member_id_required", "token_required", "member_id_required"}
