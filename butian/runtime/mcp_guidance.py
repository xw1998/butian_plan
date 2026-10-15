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
        raise ToolError('invalid_tool_arguments', {'code': 'invalid_tool_arguments', 'tool': name,
                        'issues': issues, 'next_actions': [{'tool': 'butian_tool_schema', 'arguments': {'name': name}}]})


def recovery_details(service, name, args, code):
    details = {'code': code, 'tool': name, 'next_actions': []}
    if code == 'admin_authorization_required':
        details.update(missing_conditions=['authorized_administrator'],
                       authorization={'available_to_member': False, 'grant_via_this_endpoint': False},
                       next_actions=[{'action': 'ask_authorized_operator', 'note': 'Do not impersonate another identity. Evidence registration does not create an experiment or approve it.'}])
        return details
    actor, task_id = args.get('actor_member_id'), args.get('task_id')
    # Do not reveal task state until both authentication and task visibility pass.
    if not actor or not task_id:
        return details
    try:
        service.require_member_access(actor, actor_member_id=actor, token=args.get('token'))
        if not service.store.task_access(task_id, actor):
            return details
        row = service.store.db.execute('SELECT status,revision,assignee_id,payload FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if not row:
            return details
    except (ValueError, PermissionError):
        return details
    import json
    payload = json.loads(row['payload'])
    details['current'] = {'task_id': task_id, 'status': row['status'], 'revision': row['revision'],
                          'owned_by_actor': row['assignee_id'] == actor, 'execution_mode': payload.get('execution_mode')}
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
    return details
