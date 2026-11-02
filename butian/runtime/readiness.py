"""Read-only checks; configuration availability is not execution success."""
from contextlib import closing
import sqlite3
from .local_plan import plan as local_plan


def collaboration_preflight(service, args):
    actor = args['actor_member_id']
    service.require_member_access(actor, actor_member_id=actor, token=args.get('token'))
    target = args.get('member_id', actor)
    checks = {'authentication': {'status': 'ready'}}
    # Do not inspect another member's binding or memory before authorization.
    if target != actor:
        return {'ready': False, 'execution_verified': False, 'checks': checks,
                'blockers': ['member_memory_access_denied'],
                'next_actions': [{'action': 'use_member_authenticated_connection', 'member_id': target,
                                  'note': 'This endpoint has no cross-member delegation grant.'}]}
    checks['member_memory_access'] = {'status': 'ready'}
    path = service.home / 'members' / actor / 'memory.db'
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('SELECT 1 FROM memories LIMIT 1').fetchall()
        checks['memory_readable'] = {'status': 'ready'}
    except sqlite3.Error:
        checks['memory_readable'] = {'status': 'blocked', 'reason': 'memory_unreadable'}
    try:
        provider_id, model = service._preflight_provider(actor)
        checks['provider'] = {'status': 'configured', 'provider_id': provider_id, 'model': model,
                              'execution_verified': False}
    except ValueError:
        checks['provider'] = {'status': 'blocked', 'reason': 'provider_unavailable', 'execution_verified': False}
    checks['evidence_storage'] = {'status': 'unchecked', 'reason': 'no_file_registered_or_verified'}
    if args.get('task_id'):
        task_id = args['task_id']
        if not service.store.task_access(task_id, actor):
            raise ValueError('task_access_denied')
        task = service.store.db.execute('SELECT assignee_id,status,revision,session_id FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        session = service.store.session(task['session_id'])
        checks['task'] = {'status': task['status'], 'revision': task['revision'],
                          'claimable': task['status'] == 'ready' and task['assignee_id'] == actor}
        checks['independent_review'] = {'status': 'eligible' if session.get('dispatcher_member_id') == actor and task['assignee_id'] != actor else 'blocked',
                                        'execution_verified': False}
    else:
        checks['task'] = {'status': 'unchecked', 'reason': 'task_id_required'}
        checks['independent_review'] = {'status': 'unchecked', 'reason': 'task_and_reviewer_execution_required'}
    blockers = [value.get('reason', key) for key, value in checks.items() if value['status'] == 'blocked']
    result = {'ready': False, 'configuration_ready': not blockers, 'execution_verified': False,
            'checks': checks, 'blockers': blockers,
            'next_actions': [{'action': 'configure_member_provider'}] if 'provider_unavailable' in blockers else
                            [{'tool': 'butian_tool_schema', 'arguments': {'name': 'butian_provider_start'}}],
            'remaining_verification': ['real_provider_execution', 'evidence_registration_and_hash_verification', 'independent_reviewer_execution'],
            'admin_only_operations': ['session_list', 'experiment_create'],
            'note': 'Read-only preflight does not create a task, start a Provider, grant access, or certify end-to-end readiness.'}
    if 'provider_unavailable' in blockers and args.get('goal'):
        # 没有执行后端时不再只回一句“不可用”：给一份可手动执行的本地步骤，
        # 但它不是执行结果，也不进证据链（evidence_eligible=False）。
        result['local_plan'] = local_plan(args['goal'], acceptance_criteria=args.get('acceptance_criteria'), member_id=actor)
        result['local_plan_note'] = '本地降级只是人工执行脚本：任务能否完成仍取决于真的把事做完并登记证据。'
    return result


def collaboration_start(service, args):
    """Start only the authenticated member; reuse the existing lifecycle."""
    report = collaboration_preflight(service, args)
    from .problem import normalize
    problem = normalize(args['goal'], args.get('problem'))
    if problem['definition_status'] != 'defined':
        return {'status': 'needs_clarification', 'task_created': False,
                'clarification_questions': problem['clarification_questions'], 'preflight': report}
    if not report.get('configuration_ready'):
        return {'status': 'blocked', 'task_created': False, 'preflight': report,
                'local_plan': local_plan(args['goal'], acceptance_criteria=args.get('acceptance_criteria'),
                                         member_id=args['actor_member_id']),
                'local_plan_note': '执行后端不可用但事情仍可推进：按 local_plan 手动做完并登记证据，再交付；这份计划不算完成度。'}
    result = service.submit_goal(args['goal'], member_id=args['actor_member_id'],
                                 dispatcher_member_id=args['actor_member_id'], problem=args.get('problem'))
    return {'status': result.get('execution', {}).get('status', result.get('status', 'unknown')),
            'task_created': True, 'result': result, 'independent_review': 'not_performed',
            'next_tool': 'butian_provider_consume'}
