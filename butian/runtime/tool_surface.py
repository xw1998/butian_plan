"""工具面分类与压缩表。

目的只有一个：让“这个工具是干什么的”这件事在代码里是事实，而不是靠人记。
126 个工具按用途分六类；其中与“攒经验”无关的长尾收进 ARCHIVED，
默认不出现在 tools/list 与 catalog 首页，但仍可通过 butian_tool_call 调用，
需要时可 buttian_tools_catalog(include_archived=true) 翻出来。

不改任何工具的语义、不放宽任何权限：这里只决定“默认看不看得见”。
"""

from __future__ import annotations

# 六类：分类键 -> 中文说明
CATEGORIES = {
    "experience": "经验闭环：把一次实践变成可召回、可修正、可升华为技能的经验",
    "orchestration": "任务编排：目标、任务、上下文、会话与人机问答",
    "governance": "工具治理：工具的提案、版本、审核、执行与问题单",
    "research": "研究推理：假设、实验、策略与同伴互问",
    "host": "宿主执行：把活派到别的执行器、进程或机器上跑",
    "meta": "元入口：自查目录、取 schema、按名调用",
}

CATEGORY_ORDER = ("experience", "orchestration", "governance", "research", "host", "meta")

_META_TOOLS = frozenset({"butian_tool_call", "butian_tool_schema", "butian_tools_catalog"})

_EXPERIENCE_PREFIXES = ("memory_", "knowledge_", "evolution_", "dream_", "evidence_")
_ORCHESTRATION_PREFIXES = ("task_", "goal_", "problem_", "session_", "context", "user_",
                           "dispatcher_", "collaboration_")
_RESEARCH_PREFIXES = ("hypothesis_", "experiment_", "strategy_", "peer_")
_HOST_PREFIXES = ("execution_", "provider_", "host_authorization_")
_ORCHESTRATION_EXACT = frozenset({"butian_mode", "butian_connect", "butian_status"})


def category_of(name: str) -> str:
    """按用途归类。未知名字归 meta，保证永远有答案而不是抛错。"""
    if name in _META_TOOLS:
        return "meta"
    bare = name[len("butian_"):] if name.startswith("butian_") else name
    if bare.startswith("tool_"):
        return "governance"
    if name in _ORCHESTRATION_EXACT:
        return "orchestration"
    for prefix in _EXPERIENCE_PREFIXES:
        if bare.startswith(prefix):
            return "experience"
    if bare == "claim_prepare_practice":
        return "experience"
    for prefix in _ORCHESTRATION_PREFIXES:
        if bare.startswith(prefix):
            return "orchestration"
    for prefix in _RESEARCH_PREFIXES:
        if bare.startswith(prefix):
            return "research"
    for prefix in _HOST_PREFIXES:
        if bare.startswith(prefix):
            return "host"
    return "meta"


# 长尾归档：默认不进 tools/list，也不进 catalog 首页；butian_tool_call 照常可用。
# 判据是“与攒经验这条主线无关，且属于生命周期里的分支”，不是“觉得它没用”。
ARCHIVED_TOOL_NAMES = frozenset({
    # 问题单生命周期：保留 report / issues / repair / repair_confirm / security_review，
    # 中间状态流转与统计收进长尾（11 -> 5）
    "butian_tool_issue_triage",
    "butian_tool_issue_request_information",
    "butian_tool_issue_provide_information",
    "butian_tool_issue_link_duplicate",
    "butian_tool_issue_expire",
    "butian_tool_issue_pair",
    "butian_tool_issue_statistics",
    "butian_tool_issue_escalations",
    "butian_tool_issue_escalation_claim",
    "butian_tool_issue_escalation_resolve",
    # 工作流编排与打包分享：单机闭环用不到，多机/多人时才需要
    "butian_tool_workflow_create",
    "butian_tool_workflow_execute",
    "butian_tool_workflow_verify",
    "butian_tool_workflow_retire",
    "butian_tool_bundle_export",
    "butian_tool_bundle_import",
    "butian_tool_version_add",
    "butian_tool_share",
    "butian_tool_accept",
    "butian_tool_trial",
    # 执行观测：主线只留 submit / export / cancel
    "butian_tool_execution_audit_replay",
    "butian_tool_executions_running",
    # 策略编排：五件套里只留 recommend
    "butian_strategy_compose",
    "butian_strategy_assign",
    "butian_strategy_step",
    "butian_strategy_steps",
    # 同伴互问：留 ask / answer
    "butian_peer_verify",
    "butian_peer_list",
    # 假设动作：留 grant / start
    "butian_hypothesis_action_authorize",
    "butian_hypothesis_get",
    "butian_hypothesis_observe",
    # 会话与租约长尾
    "butian_session_list",
    "butian_task_release",
    "butian_task_heartbeat",
    # 跨机授权：安全边界仍在，只是默认不占首屏
    "butian_host_authorization_grant",
    "butian_host_authorization_list",
    "butian_host_authorization_revoke",
})

# 默认暴露给宿主的最小入口：一条经验从“接到活”到“写完经验”必经的节点。
CORE_TOOL_NAMES = (
    "butian_connect",
    "butian_context",
    "butian_goal_submit",
    "butian_task_transition",
    "butian_memory_recall",
    "butian_memory_practice",
    "butian_memory_revise",
    "butian_evidence_record",
    "butian_knowledge_source_add",
    "butian_knowledge_source_read",
    "butian_tools_catalog",
    "butian_tool_schema",
    "butian_tool_call",
)

# tools/list 的排序：先入口，再按分类分组，最后按名字。
_FIRST = ("butian_connect", "butian_context", "butian_goal_submit",
          "butian_tools_catalog", "butian_tool_schema", "butian_tool_call")


def surface_order(name: str) -> tuple:
    if name in _FIRST:
        return (0, _FIRST.index(name), name)
    category = category_of(name)
    try:
        rank = CATEGORY_ORDER.index(category)
    except ValueError:  # 理论上不会发生；排序不能因此抛错
        rank = len(CATEGORY_ORDER)
    return (1, rank, name)


def describe_surface(names) -> dict:
    """给 catalog 用的分类汇总；只统计默认可见的工具。"""
    counts = {key: 0 for key in CATEGORY_ORDER}
    for name in names:
        counts[category_of(name)] = counts.get(category_of(name), 0) + 1
    return {key: {"count": counts.get(key, 0), "description": text}
            for key, text in CATEGORIES.items() if counts.get(key)}
