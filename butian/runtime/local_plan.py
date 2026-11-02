"""没有执行后端时的本地降级：把目标拆成可手动执行的工具序列。

这不是执行结果。它只按目标文本里的关键词给出一条“照做就能推进”的脚本，
不调用任何模型、不读成员私有正文、不写库；产物固定带 mode='local' 与
evidence_eligible=False，任何调用方都不得把它当作已完成的工作或证据。

以前 Provider 不可用时 preflight 只回一句 provider_unavailable 就停工，
调用方既不知道能不能先干点别的，也不知道该按什么顺序干——于是团队模式空转。
"""
from __future__ import annotations

FAMILIES = (
    {
        'keywords': ('测', '验证', '复现', 'benchmark', '评测', '回归', '跑通'),
        'matched_rule': 'verification',
        'step': {'action': 'run_and_record_the_measurement',
                 'note': '先把可重复的最小验证跑起来，记下命令、环境与原始输出；测不出来的项要写成“未测”，不要估。'},
    },
    {
        'keywords': ('修', 'bug', '报错', '异常', '崩溃', '失败'),
        'matched_rule': 'defect_reproduction',
        'step': {'action': 'reproduce_the_defect_first',
                 'note': '先稳定复现（输入、命令、观察到的错误码），再动手改；改完用同一条复现路径验证。'},
    },
    {
        'keywords': ('文档', '报告', '说明', '手册', '总结'),
        'matched_rule': 'document_deliverable',
        'step': {'action': 'write_the_deliverable_to_a_file',
                 'note': '产出落到文件（Markdown/Word），写清结论、依据与未覆盖范围；只写核对过的事实。'},
    },
    {
        'keywords': ('调研', '选型', '对比', '搜集', '查资料'),
        'matched_rule': 'evidence_collection',
        'step': {'action': 'collect_primary_sources_first',
                 'note': '先取一手来源（官方文档/规范/源码），每条结论标来源；二手转述必须回原文核对。'},
    },
)


def plan(goal, *, acceptance_criteria=None, member_id=None):
    """按关键词给出可手动执行的步骤；没有命中规则时仍给通用四步。"""
    text = str(goal or '')
    lowered = text.lower()
    steps = [{'step': 1, 'tool': 'butian_goal_submit',
              'arguments': {'goal': text.strip(), 'acceptance_criteria': list(acceptance_criteria or [])},
              'why': '先把目标登记成任务：Provider 不可用不影响登记，任务可以先以 host 方式存在。'}]
    matched = []
    for family in FAMILIES:
        if any(keyword in lowered for keyword in family['keywords']):
            matched.append(family['matched_rule'])
            steps.append({'step': len(steps) + 1, **family['step'], 'why': '目标文本命中规则 ' + family['matched_rule'] + '。'})
    steps.append({'step': len(steps) + 1, 'action': 'do_the_work_in_this_environment',
                  'note': '在当前机器上真的把事做完；不要用“已安排/已开始”替代产出。'})
    steps.append({'step': len(steps) + 1, 'tool': 'butian_evidence_record',
                  'arguments': {'path': '<真实存在的产出文件路径>'},
                  'why': '证据必须是可校验的文件；口头结论与空列表在审核里不算证据。'})
    steps.append({'step': len(steps) + 1, 'tool': 'butian_task_handoff',
                  'arguments': {'task_id': '<task_id>', 'task_revision': '<当前 revision>', 'status': 'done|partial',
                                'summary': '<实际做了什么>', 'evidence_ids': ['<ev_...>'],
                                'next_action': '<审核者下一步>', 'confidence': 0.7},
                  'why': '交付按 revision 绑定；没做完就写 partial 并说清缺口。'})
    return {
        'mode': 'local',
        'basis': 'keyword_rules',
        'evidence_eligible': False,
        'member_id': member_id,
        'goal_summary': text.strip()[:80],
        'matched_rules': matched,
        'steps': steps,
        'not_a_substitute_for': ['provider_execution', 'evidence_registration_and_verification', 'independent_review'],
        'warning': '本地规则降级：只给可手动执行的步骤，不代表任务已被执行，也不能计入证据或完成度。',
    }
