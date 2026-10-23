import pytest
from butian.runtime.problem import normalize


def test_complete_problem_definition_and_minimal_clarification():
    model = normalize('完成测量', {
        'undesired_outcomes': ['不能损坏样本'],
        'inputs': ['已标定样本'], 'outputs': ['测量报告'],
        'resources': ['测量仪'], 'deadline': '用户未指定',
        'assumptions': ['样本稳定'], 'disputes': [],
        'problem_type': 'similar', 'confirmation_required': ['确认精度要求'],
        'partial_progress_criteria': ['取样完成'],
        'acceptance_criteria': ['误差小于1%'],
    })
    assert model['definition_status'] == 'needs_clarification'
    assert model['clarification_questions'] == ['确认精度要求']
    assert model['inputs'] == ['已标定样本']
    assert model['problem_type'] == 'similar'
    ready = normalize('完成测量', {'acceptance_criteria': ['误差小于1%']})
    assert ready['definition_status'] == 'defined'
    incomplete = normalize('完成测量')
    assert incomplete['definition_status'] == 'needs_clarification'
    assert incomplete['clarification_questions'] == ['什么结果可以证明目标已完成？']


@pytest.mark.parametrize('changes', [
    {'problem_type': 'invented'}, {'deadline': 123}, {'inputs': '猜测输入'},
    {'assumptions': ['']}, {'confirmation_required': [None]},
])
def test_invalid_definition_rejected(changes):
    with pytest.raises(ValueError):
        normalize('完成测量', changes)


def test_incomplete_production_goal_never_starts_provider(tmp_path):
    from butian.runtime.service import ButianService
    service = ButianService(tmp_path)
    try:
        service.add_member('owner', '目标负责人')
        result = service.submit_goal('完成测量', member_id='owner', problem={'confirmation_required': ['精度要求是多少？']})
        assert result['status'] == 'needs_clarification'
        assert result['problem']['clarification_questions'] == ['精度要求是多少？', '什么结果可以证明目标已完成？']
        assert service.store.db.execute('SELECT COUNT(*) FROM provider_sessions').fetchone()[0] == 0
        task = service.store.db.execute('SELECT payload,status FROM tasks WHERE task_id=?', (result['task_id'],)).fetchone()
        assert task['status'] == 'todo'
    finally:
        service.close()


def test_clarification_revision_resumes_real_execution(tmp_path):
    import json
    import sys
    from butian.runtime.service import ButianService
    from butian.runtime.problem import ProblemModels
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': [sys.executable, '-c', 'import sys; print(sys.stdin.read())']}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        service.add_member('owner', '执行者', provider_id='local-command', model_id='local-command')
        result = service.submit_goal('完成测量', member_id='owner')
        task_id = result['task_id']
        ProblemModels(service.store).update(task_id, 1, {'acceptance_criteria': ['报告包含测量值'], 'confirmation_required': []}, actor_id='owner', reason='用户明确验收')
        service.resume_problem_task(task_id, actor_member_id='owner')
        result = service.run_solo_task(task_id, actor_member_id='owner')
        # 带上整份结果：偶发的 unknown 必须能一眼看到 reason，不靠猜。
        assert result['status'] == 'completed', result
        payload = json.loads(service.store.db.execute('SELECT payload FROM tasks WHERE task_id=?', (task_id,)).fetchone()[0])
        assert '报告包含测量值' in payload['provider_output']
        assert payload['problem_model']['revision'] == 2
    finally:
        service.close()


def test_standard_host_resume_is_authenticated_and_model_gated(tmp_path):
    from butian.runtime.service import ButianService
    from butian.mcp_server import call_tool
    service = ButianService(tmp_path)
    try:
        service.add_member('host', '宿主')
        service.add_member('other', '无关成员')
        service.store.set_member_token('host', 'host-test-token')
        service.store.set_member_token('other', 'other-test-token')
        auth = {'actor_member_id': 'host', 'token': 'host-test-token'}
        created = call_tool(service, 'butian_goal_submit', dict(auth, goal='核验测量'))
        task = created['task_id']
        with pytest.raises(ValueError, match='problem_needs_clarification'):
            call_tool(service, 'butian_problem_resume', dict(auth, task_id=task))
        call_tool(service, 'butian_problem_update', dict(auth, task_id=task, expected_revision=1, changes={'acceptance_criteria': ['误差小于1%']}, reason='确认验收'))
        with pytest.raises(ValueError, match='task_access_denied'):
            call_tool(service, 'butian_problem_resume', {'actor_member_id': 'other', 'token': 'other-test-token', 'task_id': task})
        resumed = call_tool(service, 'butian_problem_resume', dict(auth, task_id=task))
        assert resumed['status'] == 'ready'
        assert service.store.db.execute('SELECT COUNT(*) FROM provider_sessions').fetchone()[0] == 0
    finally:
        service.close()


def test_raw_transition_cannot_bypass_problem_clarification(tmp_path):
    from butian.runtime.service import ButianService
    from butian.mcp_server import call_tool
    service = ButianService(tmp_path)
    try:
        service.add_member('host', '宿主')
        service.store.set_member_token('host', 'host-test-token')
        auth = {'actor_member_id': 'host', 'token': 'host-test-token'}
        created = call_tool(service, 'butian_goal_submit', dict(auth, goal='未定义验收'))
        task = created['task_id']
        with pytest.raises(ValueError, match='problem_needs_clarification'):
            call_tool(service, 'butian_task_transition', dict(auth, task_id=task, expected_revision=1, target='ready', operation_key='bypass-test'))
        assert service.store.db.execute('SELECT status FROM tasks WHERE task_id=?', (task,)).fetchone()[0] == 'todo'
    finally:
        service.close()


def test_running_correction_stops_old_process_and_allows_new_revision(tmp_path):
    import json
    import sys
    from butian.runtime.service import ButianService
    from butian.mcp_server import call_tool
    command = [sys.executable, '-c', 'import sys,time; text=sys.stdin.read(); time.sleep(20) if "旧条件" in text else None; print(text)']
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': command}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        service.add_member('owner', '执行者', provider_id='local-command', model_id='local-command')
        service.store.set_member_token('owner', 'owner-test-token')
        auth = {'actor_member_id': 'owner', 'token': 'owner-test-token'}
        goal = service.submit_goal('完成测量', member_id='owner', problem={'constraints': ['旧条件'], 'acceptance_criteria': ['留存报告']})
        task = goal['task_id']
        old = goal['execution']['provider_session_id']
        revised = call_tool(service, 'butian_problem_update', dict(auth, task_id=task, expected_revision=1, changes={'constraints': ['新条件']}, reason='用户纠正条件'))
        assert revised['revision'] == 2
        assert service.store.provider_session(old)['status'] == 'cancelled'
        state = service.store.db.execute('SELECT status,lease_id FROM tasks WHERE task_id=?', (task,)).fetchone()
        assert state['status'] == 'ready' and state['lease_id'] is None
        with pytest.raises(ValueError):
            service.consume_provider_task(old, actor_member_id='owner')
        call_tool(service, 'butian_problem_resume', dict(auth, task_id=task))
        result = service.run_solo_task(task, actor_member_id='owner')
        assert result['status'] == 'completed'
        payload = json.loads(service.store.db.execute('SELECT payload FROM tasks WHERE task_id=?', (task,)).fetchone()[0])
        assert '新条件' in payload['provider_output']
    finally:
        service.close()
