"""Content-free MCP validation and recovery hints; never relax authorization."""
from __future__ import annotations
import copy


class ToolError(ValueError):
    def __init__(self, code, details):
        super().__init__(code)
        self.details = details


def describe_tool(name, tools):
    tool = next((tool for tool in tools if tool['name'] == name), None)
    if tool is None:
        raise ToolError('unknown_tool', {'code': 'unknown_tool', 'next_actions': [{'tool': 'butian_tools_catalog'}]})
    result = copy.deepcopy(tool)
    if name.startswith('butian_experiment_'):
        result['authorization'] = {'available_to_member': False, 'reason': 'admin_authorization_required',
                                   'note': 'This MCP member endpoint has no administrator grant flow. Use an authorized operator; do not retry with a different member or token.'}
    else:
        result['authorization'] = {'note': 'Existing member, task visibility, ownership and review checks still apply.'}
    return result


def validate_arguments(name, args, tools):
    tool = next((tool for tool in tools if tool['name'] == name), None)
    if tool is None:
        describe_tool(name, tools)
    if not isinstance(args, dict):
        raise ToolError('invalid_tool_arguments', {'code': 'invalid_tool_arguments', 'expected': 'object'})
    schema = tool['inputSchema']
    issues = []
    for key in schema.get('required', []):
        if key not in args:
            issues.append({'field': key, 'reason': 'required'})
    checks = {'string': lambda v: isinstance(v, str), 'integer': lambda v: type(v) is int,
              'number': lambda v: type(v) in (int, float), 'boolean': lambda v: type(v) is bool,
              'array': lambda v: isinstance(v, list), 'object': lambda v: isinstance(v, dict)}
    for key, spec in schema.get('properties', {}).items():
        if key not in args:
            continue
        value = args[key]
        kind = spec.get('type')
        if kind in checks and not checks[kind](value):
            issues.append({'field': key, 'reason': 'type', 'expected': kind})
            continue
        if 'enum' in spec and value not in spec['enum']:
            issues.append({'field': key, 'reason': 'enum', 'allowed': spec['enum']})
        if kind in ('integer', 'number'):
            if ('minimum' in spec and value < spec['minimum']) or ('maximum' in spec and value > spec['maximum']):
                issues.append({'field': key, 'reason': 'range', **{k: spec[k] for k in ('minimum', 'maximum') if k in spec}})
    if issues:
        missing = [issue['field'] for issue in issues if issue.get('reason') == 'required']
        identity_fields = ('member_id', 'actor_member_id', 'token')
        next_actions = [{'tool': 'butian_tool_schema', 'arguments': {'name': name}}]
        if any(field in identity_fields for field in missing):
            next_actions.insert(0, {'tool': 'butian_connect', 'arguments': {'member_id': '<成员 id>'},
                                    'why': '身份由连接建立后自动注入，连一次就不必每次重传 member_id/actor_member_id/token。'})
        if missing and all(issue.get('reason') == 'required' and issue['field'] in identity_fields for issue in issues):
            # 只缺身份时给服务层同名的语义错误：invalid_tool_arguments 会让人以为是参数拼错。
            code = missing[0] + '_required'
            raise ToolError(code, {'code': code, 'tool': name, 'issues': issues,
                                   'missing_conditions': ['connected_member_identity'],
                                   'next_actions': next_actions})
        raise ToolError('invalid_tool_arguments', {'code': 'invalid_tool_arguments', 'tool': name,
                        'issues': issues, 'next_actions': next_actions})


# “对象不存在”类错误的通用出路：告诉调用方去哪里看见/建立这个对象，
# 而不是回一个空 next_actions 让 AI 自己猜。
_EXISTENCE_HINTS = {
    'knowledge_source_not_found': ('knowledge_source_registered', [
        {'tool': 'butian_knowledge_source_queue', 'why': '先看当前成员已登记或已排队的源与 source_id。'},
        {'tool': 'butian_knowledge_source_add', 'why': '还没有这个源就先登记 locator 与 content_hash。'}]),
    'source_snapshot_not_found': ('readable_source_snapshot', [
        {'tool': 'butian_knowledge_source_verify',
         'why': 'add 只登记哈希、不存正文；把原文 content 交给 verify，哈希码一致后才会存成可读快照。'},
        {'tool': 'butian_knowledge_source_capture', 'why': '若原文在公网，可 capture 该 URL 抓取并校验后登记。'}]),
    'tool_version_not_found': ('published_tool_version', [
        {'tool': 'butian_tool_list', 'why': '先看自己有哪些工具与版本。'},
        {'tool': 'butian_tool_version_add', 'why': '版本还不存在时先提交 content 与 manifest。'}]),
    'tool_not_found': ('owned_tool', [{'tool': 'butian_tool_list', 'why': '确认工具属于当前成员且未被归档。'}]),
    'memory_not_found': ('owned_memory', [
        {'tool': 'butian_memory_search', 'why': '确认这条记忆是否属于当前成员（记忆是成员隔离的）。'},
        {'tool': 'butian_context', 'why': '先看当前连接的是哪个成员。'}]),
    'claim_not_found': ('registered_claim', [
        {'tool': 'butian_knowledge_source_read', 'why': '先读源，确认 claim_id 来自这个源。'}]),
    'evidence_not_found': ('registered_evidence', [
        {'tool': 'butian_evidence_record', 'why': '证据必须先登记再引用。'}]),
    'reviewer_member_not_found': ('existing_member', [
        {'tool': 'butian_context', 'why': '看成员列表里有哪些成员 id。'}]),
    'history_recovery_selector_required': ('exactly_one_selector', [
        {'tool': 'butian_context_recover',
         'arguments': {'task_id': '<task_id>', 'event_id': '<committed event id>'},
         'why': 'event_id 与 payload_field 必须二选一：查历史事件给 event_id，查已落盘输出给 payload_field（provider_output/results/...）。'},
        {'tool': 'butian_context', 'why': '先拿到 task_id 与 revision。'}]),
    'distillation_requires_multiple_memories_and_summary': ('at_least_two_memories_plus_summary', [
        {'tool': 'butian_memory_search', 'why': '先找出两条以上相关记忆的 memory_id。'},
        {'tool': 'butian_dream_history', 'why': '看已有蒸馏记录，避免重复提炼。'}]),
    'team_session_required': ('team_mode_session', [
        {'tool': 'butian_goal_submit',
         'arguments': {'goal': '<目标>', 'acceptance_criteria': ['<可验证标准>'], 'member_ids': ['<成员 id>']},
         'why': 'team_run 只能汇总团队目标；单人目标不需要汇总。'},
        {'tool': 'butian_context', 'why': '看当前会话是 solo 还是 team。'}]),
    'catalog_query_no_match': ('matching_catalog_entry', [
        {'tool': 'butian_tools_catalog', 'why': '不传 query 翻页看全部工具名。'}]),
    'unknown_argument': ('accepted_argument_names', [
        {'tool': 'butian_tool_schema', 'why': '按 details.accepted 里的名字重传；拼错的参数不会被静默忽略。'}]),
    'problem_field_conflict': ('one_source_per_problem_field', [
        {'tool': 'butian_problem_get', 'why': '先看已保存的问题定义，再决定改哪一处。'}]),
    'unknown_problem_field': ('known_problem_fields', [
        {'tool': 'butian_problem_get', 'why': '改动问题定义前先读现值；字段名见 butian_tool_schema(butian_goal_submit)。'}]),
}

def _fallback_details(details, name, code):
    """没有专门脚本时也要给出可执行的下一步；绝不留空 next_actions。"""
    hint = _EXISTENCE_HINTS.get(code)
    if hint:
        details['missing_conditions'] = [hint[0]]
        details['next_actions'] = hint[1]
        if code.endswith('_not_found'):
            details.setdefault('note', '对象不存在或不属于当前成员：先确认来源，再重试。')
        return details
    details['next_actions'] = [
        {'tool': 'butian_tool_schema', 'arguments': {'name': name}, 'why': '看这个工具要求的前置条件与参数。'},
        {'tool': 'butian_context', 'why': '确认当前成员与任务状态后重试；不要靠猜参数绕过。'}]
    details.setdefault('note', '没有专门的恢复脚本：先确认对象存在且属于当前成员。')
    return details

def recovery_details(service, name, args, code):
    details = {'code': code, 'tool': name, 'next_actions': []}
    if code == 'admin_authorization_required':
        details.update(missing_conditions=['authorized_administrator'],
                       authorization={'available_to_member': False, 'grant_via_this_endpoint': False},
                       next_actions=[{'action': 'ask_authorized_operator', 'note': 'Do not impersonate another identity. Evidence registration does not create an experiment or approve it.'}])
        return details
    if code in {'member_memory_access_denied', 'member_access_denied', 'member_task_access_denied',
                'member_authentication_failed', 'member_id_required',
                'actor_member_id_required', 'token_required'}:
        actor, target = args.get('actor_member_id'), args.get('member_id')
        identity_only = code in {'member_id_required', 'actor_member_id_required', 'token_required'}
        details.update(missing_conditions=['connected_member_identity' if identity_only else 'member_boundary'])
        if code != 'member_authentication_failed' and (actor or target):
            # 认证失败时不回显身份/状态：口令错了先别告诉对方“你是谁”。
            details['current'] = {'actor_member_id': actor, 'requested_member_id': target}
        if identity_only:
            details['next_actions'] = [{'tool': 'butian_connect', 'arguments': {'member_id': '<one of the members returned by butian_context>'},
                                        'why': '成员类工具要么显式传 member_id/actor_member_id，要么先 butian_connect 绑定身份（连一次后身份自动注入）。'}]
        elif target and actor and target != actor:
            details['next_actions'] = [{'tool': 'butian_connect', 'arguments': {'member_id': target},
                                        'why': '该操作只对成员本人开放：要替它做事就先以它的身份连接（远程还需该成员口令），不要用换参数的方式绕过。'},
                                       {'action': 'ask_member_to_run', 'note': '跨成员执行应由该成员自己的连接完成，结果不能算到当前成员名下。'}]
        else:
            details['next_actions'] = [{'tool': 'butian_context', 'why': '先确认当前连接的身份与可见范围。'}]
        return details
    if code in {'task_access_denied', 'task_not_found', 'task_visibility_denied'}:
        # 看不到这个任务时不回显任何任务状态（安全契约）。
        details.update(missing_conditions=['task_visibility_for_current_member'],
                       next_actions=[{'tool': 'butian_context', 'why': '先确认当前连接身份能看到哪些任务；跨成员任务不可见时不要靠猜 task_id 重试。'},
                                     {'tool': 'butian_task_list', 'why': '列出当前成员可见的任务做对照。'}])
        return details
    actor, task_id = args.get('actor_member_id'), args.get('task_id')
    # Do not reveal task state until both authentication and task visibility pass.
    if not actor or not task_id:
        return _fallback_details(details, name, code)
    try:
        service.require_member_access(actor, actor_member_id=actor, token=args.get('token'))
        if not service.store.task_access(task_id, actor):
            return _fallback_details(details, name, code)
        row = service.store.db.execute('SELECT status,revision,assignee_id,payload FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if not row:
            return _fallback_details(details, name, code)
    except Exception:
        # 这里只是补充提示：任何探测失败都不能把一条干净的业务错误变成崩溃。
        return _fallback_details(details, name, code)
    import json
    payload = json.loads(row['payload'])
    details['current'] = {'task_id': task_id, 'status': row['status'], 'revision': row['revision'],
                          'owned_by_actor': row['assignee_id'] == actor, 'execution_mode': payload.get('execution_mode')}
    if code == 'handoff_evidence_required_for_done':
        details['missing_conditions'] = ['valid_evidence_for_approval']
        details['next_actions'] = [
            {'tool': 'butian_evidence_record', 'arguments': {'path': '<真实存在的文件路径>'},
             'why': '审核只认已登记并校验过的 evidence_id，口头结论与空列表不算证据。'},
            {'tool': 'butian_task_handoff',
             'arguments': {'task_id': task_id, 'task_revision': row['revision'], 'status': 'done|partial',
                           'summary': '<实际做了什么>', 'evidence_ids': ['<ev_...>'],
                           'next_action': '<审核者下一步>', 'confidence': 0.7},
             'why': '交付按 revision 绑定：补好证据后仍要在当前 revision 重新交付，再走审核。'},
        ]
        return details
    if code == 'handoff_required_for_done':
        # 审核要求“当前 revision 上有交付”，而迁状态会 +1 revision，这里把正确顺序写清楚。
        import json as _json
        latest = service.store.db.execute(
            'SELECT revision FROM handoffs WHERE task_id=? ORDER BY revision DESC LIMIT 1', (task_id,)).fetchone()
        details['current'].update({'handoff_revision': latest['revision'] if latest else None,
                                   'review_requires_handoff_at_revision': row['revision']})
        details['missing_conditions'] = ['handoff_at_current_revision']
        actions = []
        if row['status'] == 'running':
            actions.append({'tool': 'butian_task_transition',
                            'arguments': {'task_id': task_id, 'expected_revision': row['revision'], 'target': 'review'},
                            'why': '审核只发生在 review；迁过去后 revision 变为 ' + str(row['revision'] + 1) + '。'})
            target_revision = row['revision'] + 1
        elif row['status'] == 'review':
            target_revision = row['revision']
        else:
            target_revision = row['revision']
        actions.append({'tool': 'butian_task_handoff',
                        'arguments': {'task_id': task_id, 'task_revision': target_revision,
                                      'status': 'done|partial', 'summary': '<实际做了什么>',
                                      'evidence_ids': ['<ev_...>'], 'next_action': '<审核者下一步>', 'confidence': 0.7},
                        'why': '交付按 revision 绑定：审核只认当前 revision 上的交付，旧 revision 的交付不算数。'})
        details['next_actions'] = actions
        return details
    if code == 'problem_needs_clarification':
        # 问题定义没完成时最容易被卡死：这里给出问题 revision、待答问题与可照抄的修改动作。
        import json as _json
        try:
            model_row = service.store.db.execute(
                'SELECT body FROM problem_models WHERE task_id=? ORDER BY revision DESC LIMIT 1', (task_id,)).fetchone()
        except Exception:
            model_row = None
        model = _json.loads(model_row['body']) if model_row else {}
        details['current'].update({'definition_status': model.get('definition_status'),
                                   'problem_revision': model.get('revision')})
        details['missing_conditions'] = ['problem_definition']
        details['clarification_questions'] = model.get('clarification_questions') or []
        details['required'] = {'definition_status': 'defined', 'acceptance_criteria': 'non-empty list'}
        details['next_actions'] = [
            {'tool': 'butian_problem_get', 'arguments': {'task_id': task_id},
             'why': '先看当前问题定义与 revision，别凭记忆改。'},
            {'tool': 'butian_problem_update',
             'arguments': {'task_id': task_id, 'expected_revision': model.get('revision', 0),
                           'changes': {'acceptance_criteria': ['<可验证的验收标准>']},
                           'reason': '<为什么这样补>'},
             'note': 'expected_revision 是问题定义 revision（上面 problem_revision），不是任务 revision；changes 只写要改的问题字段。'},
        ]
        return details
    if code.startswith('invalid task transition'):
        from ..core.state_machine import allowed_targets
        status = row['status']
        details['missing_conditions'] = ['current_status_allows_target']
        details['required'] = {'current_status': status, 'allowed_targets': allowed_targets(status)}
        if status == 'todo':
            details['next_actions'] = [{'tool': 'butian_task_transition',
                                        'arguments': {'task_id': task_id, 'expected_revision': row['revision'], 'target': 'ready'},
                                        'why': 'todo 只能先迁到 ready，然后才认领。'}]
        elif status == 'ready':
            details['next_actions'] = [{'tool': 'butian_task_claim', 'arguments': {'task_id': task_id},
                                        'why': 'ready 状态请用 claim，不要直接 transition 到 claimed。'}]
        elif status == 'claimed':
            details['next_actions'] = [{'tool': 'butian_task_transition',
                                        'arguments': {'task_id': task_id, 'expected_revision': row['revision'], 'target': 'running'},
                                        'why': '已认领，迁到 running 表示开始执行。'}]
        elif status == 'review':
            details['next_actions'] = [{'tool': 'butian_task_review',
                                        'arguments': {'task_id': task_id, 'revision': row['revision'], 'verdict': 'approve|rework'},
                                        'why': 'review 状态由获授权审核者走审核接口，不要改成 done。'}]
        else:
            details['next_actions'] = [{'tool': 'butian_task_list', 'why': '先看当前状态再决定，不要强推终态任务。'}]
        return details
    if code == 'external_execution_required':
        details['missing_conditions'] = ['execution_mode_host_or_manual_provider']
        details['next_actions'] = [{'tool': 'butian_goal_submit', 'note': 'For a new host-executed task, submit the goal and acceptance criteria through this entry. Do not silently recreate an existing task.'},
                                   {'tool': 'butian_provider_start', 'note': 'For provider execution, use the configured provider instead of external export.'}]
    elif code in {'handoff_task_not_owned_or_active', 'external_task_not_running', 'execution_owner_required'}:
        details['missing_conditions'] = ([] if row['assignee_id'] == actor else ['task_ownership']) + ([] if row['status'] in {'running', 'review'} else ['active_task_state'])
        details['required'] = {'owned_by_actor': True, 'status': ['running', 'review'] if name == 'butian_task_handoff' else ['running']}
        if row['assignee_id'] != actor:
            details['missing_conditions'] = ['task_ownership']
            details['next_actions'] = [{'action': 'contact_task_dispatcher', 'note': 'Do not change identity to bypass ownership.'}]
        elif row['status'] == 'todo':
            details['next_actions'] = [{'tool': 'butian_task_transition', 'arguments': {'task_id': task_id, 'expected_revision': row['revision'], 'target': 'ready'}, 'missing_arguments': ['operation_key'], 'note': 'Then claim the ready task and transition claimed to running using the refreshed revision.'}]
        elif row['status'] == 'ready':
            details['next_actions'] = [{'tool': 'butian_task_claim', 'arguments': {'task_id': task_id, 'member_id': actor}}]
        elif row['status'] == 'claimed':
            details['next_actions'] = [{'tool': 'butian_task_transition', 'arguments': {'task_id': task_id, 'expected_revision': row['revision'], 'target': 'running'}, 'missing_arguments': ['operation_key']}]
        else:
            details['next_actions'] = [{'tool': 'butian_task_list', 'note': 'Inspect current state before retrying; never force a terminal task to running.'}]
    elif code.startswith('handoff_'):
        details['missing_conditions'] = ['valid_handoff_payload']
        details['next_actions'] = [
            {'tool': 'butian_tool_schema', 'arguments': {'name': 'butian_task_handoff'}},
            {'tool': 'butian_context', 'why': 'task_revision 必须等于当前 revision；证据要先 butian_evidence_record 再引用 evidence_id。'},
        ]
    if not details['next_actions']:
        _fallback_details(details, name, code)
    return details
