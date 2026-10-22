"""AI 易用层：一次接上身份、一个开关切模式、一次调用看全上下文。

设计约束：
- 只在调用方显式允许时有本地信任（BUTIAN_MCP_TOKEN_REQUIRED 未设为 1）；
- 永远不推测成员身份，不跨成员读取任务或记忆；
- 任何便利都不改变任务状态机、权限边界和审核独立性。
"""
from __future__ import annotations

import json
import os

MODE_SETTING = "default_session_mode"
MODES = ("solo", "team")
ACTIVE_STATUSES = ("todo", "ready", "claimed", "running", "review", "unknown")


def token_required() -> bool:
    """默认本机免口令；共享部署可设 BUTIAN_MCP_TOKEN_REQUIRED=1 收紧。"""
    return os.environ.get("BUTIAN_MCP_TOKEN_REQUIRED", "0") == "1"


def current_mode(service) -> str:
    mode = service.store.get_setting(MODE_SETTING, "solo")
    return mode if mode in MODES else "solo"


def set_current_mode(service, mode: str, *, actor: str | None = None, session_id: str | None = None) -> dict:
    if mode not in MODES:
        raise ValueError("invalid_session_mode")
    previous = current_mode(service)
    service.store.set_setting(MODE_SETTING, mode)
    result = {"mode": mode, "previous_mode": previous, "applies_to": "new_goals", "session": None}
    if session_id is None and actor:
        row = service.store.db.execute(
            "SELECT session_id FROM sessions WHERE dispatcher_member_id=? ORDER BY created_at DESC LIMIT 1",
            (actor,)).fetchone()
        session_id = row["session_id"] if row else None
    if session_id:
        switched = service.switch_session_mode(session_id, mode, current_member_id=actor)
        result["session"] = switched
    return result


def connect(service, args: dict, state: dict) -> dict:
    """绑定或切换当前连接的成员身份；本机默认不需要口令。"""
    if not isinstance(state, dict):
        raise ValueError("session_state_unavailable")
    member = args.get("member_id") or args.get("actor_member_id") or state.get("actor")
    if not member:
        rows = service.store.db.execute("SELECT member_id FROM members ORDER BY member_id").fetchall()
        if len(rows) != 1:
            return {"connected": False, "reason": "member_id_required",
                    "members": [row["member_id"] for row in rows],
                    "next_actions": [{"tool": "butian_connect", "arguments": {"member_id": "<choose>"}}],
                    "note": "存在多个成员时必须显式指定，不会替你猜。"}
        member = rows[0]["member_id"]
    if not service.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (member,)).fetchone():
        raise ValueError("member_not_found")
    token = args.get("token")
    if token_required():
        if not service.store.authenticate_member(member, token):
            raise ValueError("member_authentication_failed")
        service.local_actor = None
    else:
        service.enable_local_actor(member)
    state["actor"], state["token"] = member, token
    return {"connected": True, "actor_member_id": member, "token_required": token_required(),
            "mode": current_mode(service),
            "next_actions": [{"tool": "butian_context"}, {"tool": "butian_mode", "arguments": {"action": "get"}}]}


_STEP_BY_STATUS = {
    "todo": ("butian_task_transition", "ready", "任务已建好但未就绪：先迁到 ready，之后才能被认领。"),
    "ready": ("butian_task_claim", None, "任务已就绪：认领后进入 claimed，lease 到期会自动释放。"),
    "claimed": ("butian_task_transition", "running", "已认领：迁到 running 表示开始执行。"),
    "running": ("butian_task_transition", "review", "执行完成：迁到 review 交给其他成员独立审核。"),
}


def next_step_for_task(task: dict, actor: str | None) -> dict | None:
    """给出该任务下一条合法的迁移动作，参数直接可复制，不用宿主去猜 revision 与 operation_key。"""
    assignee = task.get("assignee_id")
    if actor and assignee and assignee != actor:
        # 派发者不代做别人的任务，否则独立执行与独立审核都会失效。
        return None
    step = _STEP_BY_STATUS.get(task.get("status"))
    if not step:
        return None
    tool, target, why = step
    if tool == "butian_task_claim":
        return {"tool": tool, "arguments": {"task_id": task["task_id"]}, "why": why}
    return {"tool": tool,
            "arguments": {"task_id": task["task_id"], "expected_revision": task["revision"],
                          "target": target,
                          "operation_key": f"{task['task_id']}:{task['revision']}:{target}"},
            "why": why}


def build_context(service, *, actor: str | None, limit: int = 20) -> dict:
    """一次返回模式、成员、可见任务和下一步动作，免去宿主自己拼 session_id 与 revision。"""
    members = [dict(row) for row in service.store.db.execute(
        "SELECT member_id, name, role, provider_id FROM members ORDER BY member_id")]
    visible = []
    for task in service.store.list_tasks():
        if actor and not service.store.task_access(task["task_id"], actor):
            continue
        payload = {}
        try:
            payload = json.loads(task.get("payload") or "{}")
        except ValueError:
            payload = {}
        visible.append({"task_id": task["task_id"], "title": task["title"], "status": task["status"],
                        "revision": task["revision"], "assignee_id": task["assignee_id"],
                        "session_id": task["session_id"], "mode": payload.get("mode"),
                        "execution_mode": payload.get("execution_mode")})
    active = [task for task in visible if task["status"] in ACTIVE_STATUSES]
    recent = (active + [task for task in visible if task["status"] not in ACTIVE_STATUSES])[-limit:]
    recent.reverse()
    sessions = [dict(row) for row in service.store.db.execute(
        "SELECT session_id, mode, dispatcher_member_id, created_at FROM sessions ORDER BY created_at DESC LIMIT 5")]
    mode = current_mode(service)
    others = [member["member_id"] for member in members if member["member_id"] != actor]
    next_actions = []
    if actor is None:
        next_actions.append({"tool": "butian_connect", "arguments": {},
                             "why": "先确定当前成员身份，后续调用不必重复传。"})
    # 已有任务时优先推进它，而不是再建一个目标：只给最靠前两条推进动作，避免刷屏。
    advancing = 0
    for task in active:
        if advancing >= 2:
            break
        step = next_step_for_task(task, actor)
        if step:
            next_actions.append(step)
            advancing += 1
    review_tasks = [task for task in active if task["status"] == "review" and task.get("assignee_id") != actor]
    if review_tasks:
        target = review_tasks[0]
        next_actions.append({"tool": "butian_task_review",
                             "arguments": {"task_id": target["task_id"], "revision": target["revision"],
                                           "verdict": "approve|rework"},
                             "why": "有任务等待审核；审核必须由有权限的其他成员独立完成，不能自己审自己的任务。"})
    own_review = [task for task in active if task["status"] == "review" and task.get("assignee_id") == actor]
    if own_review and not review_tasks:
        next_actions.append({"note": "你名下的任务已在 review，需由其他成员审核；不要自己审自己。",
                             "task_id": own_review[0]["task_id"]})
    if mode == "team" and not others:
        next_actions.append({"outside_mcp": "butian member add <member_id> <name> --role <role>",
                             "why": "当前只有你一个成员，团队模式会退化；本 MCP 成员面无添加成员工具，需用 CLI 或网页添加。"})
    waiting_others = [task for task in active
                      if task.get("assignee_id") and task["assignee_id"] != actor
                      and task["status"] in {"todo", "ready", "claimed", "running"}]
    if not advancing and waiting_others:
        target = waiting_others[0]
        next_actions.append({"tool": "butian_task_tree", "arguments": {"task_id": target["task_id"]},
                             "why": f"任务由 {target['assignee_id']} 负责，派发者不代做；可查看进度或等其完成后审核。"})
    if not advancing and not waiting_others:
        # 没有可推进的任务时才提示建新目标，否则会诱导不停建任务却永不开工。
        if mode == "solo":
            next_actions.append({"tool": "butian_goal_submit", "arguments": {"goal": "<your goal>"},
                                 "why": "当前没有待推进任务。单人模式：目标会交给当前成员，宿主直接执行。"})
        else:
            next_actions.append({"tool": "butian_goal_submit",
                                 "arguments": {"goal": "<your goal>", "member_ids": others or ["<member>"]},
                                 "why": "当前没有待推进任务。团队模式：不给 subtasks 时按成员职责生成确定性分工，可由宿主覆盖。"})
    return {"actor_member_id": actor, "mode": mode, "members": members, "candidate_workers": others,
            "sessions": sessions, "tasks": recent, "active_task_count": len(active),
            "token_required": token_required(), "next_actions": next_actions}


def role_split_plan(service, goal: str, workers: list[str]) -> list[str]:
    """按成员职责生成确定性分工；不调用模型，不编造实验结论。"""
    plans = []
    for worker in workers:
        row = service.store.db.execute(
            "SELECT name, role, responsibilities FROM members WHERE member_id=?", (worker,)).fetchone()
        if not row:
            raise ValueError(f"member_not_found:{worker}")
        try:
            duties = json.loads(row["responsibilities"] or "[]")
        except ValueError:
            duties = []
        duty = "；".join(str(item) for item in duties) or row["role"] or "成员职责"
        plans.append(
            f"目标：{goal}\n成员：{row['name']}（{row['role']}）\n职责：{duty}\n"
            "要求：独立完成职责范围内的可核验工作，给出结论、证据与已知局限；"
            "不得代替其他成员，不得编造未执行的实验或数据。")
    return plans
