import pytest
from butian.runtime.service import ButianService
from butian.mcp_server import call_tool


def test_host_goal_model_correction_restart(tmp_path):
    service = ButianService(tmp_path)
    service.add_member('host', '宿主执行者')
    service.store.set_member_token('host', 'test-model-token')
    auth = {'actor_member_id': 'host', 'token': 'test-model-token'}
    try:
        result = call_tool(service, 'butian_goal_submit', dict(auth, goal='完成测量', problem={'constraints': ['不可发布'], 'known_facts': ['样本已取得'], 'unknowns': ['误差上限'], 'acceptance_criteria': []}))
        task = result['task_id']
        assert result['status'] == 'needs_clarification'
        assert result['problem']['clarification_questions'] == ['什么结果可以证明目标已完成？']
        first = call_tool(service, 'butian_problem_get', dict(auth, task_id=task))
        assert first['goal'] == '完成测量' and first['revision'] == 1
        assert first['unknowns'] == ['误差上限']
        with pytest.raises(ValueError, match='revision_conflict'):
            call_tool(service, 'butian_problem_update', dict(auth, task_id=task, expected_revision=9, changes={'unknowns': []}, reason='用户补充'))
        revised = call_tool(service, 'butian_problem_update', dict(auth, task_id=task, expected_revision=1, changes={'unknowns': [], 'acceptance_criteria': ['误差小于1%']}, reason='用户确认'))
        assert revised['revision'] == 2 and revised['constraints'] == ['不可发布']
        assert revised['definition_status'] == 'defined'
        with pytest.raises(ValueError):
            call_tool(service, 'butian_problem_get', dict(auth, token='wrong', task_id=task))
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        restored = call_tool(service, 'butian_problem_get', dict(auth, task_id=task))
        assert restored == revised
        text, _ = service.compose_context('host', '继续', task_id=task)
        assert '误差小于1%' in text and '不可发布' in text
        assert not (tmp_path / 'providers.json').exists()
    finally:
        service.close()


def test_correction_invalidates_execution_revision_and_is_atomic(tmp_path):
    from butian.runtime.problem import ProblemModels, normalize
    from concurrent.futures import ThreadPoolExecutor
    service = ButianService(tmp_path)
    try:
        service.add_member('owner', '负责人')
        session = service.store.create_session('solo', 'owner', None, {})
        task = service.store.create_task(session, '原目标', assignee_id='owner')
        models = ProblemModels(service.store)
        models.save(task, normalize('原目标'), actor_id='owner', reason='目标输入')
        before = service.store.db.execute('SELECT revision FROM tasks WHERE task_id=?', (task,)).fetchone()[0]
        def correct(value):
            try:
                return models.update(task, 1, {'constraints': [value]}, actor_id='owner', reason='用户纠正')['revision']
            except ValueError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(correct, ['约束A', '约束B']))
        assert sorted(map(str, outcomes)) == ['2', 'revision_conflict']
        after = service.store.db.execute('SELECT revision FROM tasks WHERE task_id=?', (task,)).fetchone()[0]
        assert after == before + 1
        assert service.store.db.execute('SELECT COUNT(*) FROM problem_models WHERE task_id=?', (task,)).fetchone()[0] == 2
    finally:
        service.close()


def test_standard_stdio_goal_without_provider(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    service = ButianService(tmp_path)
    service.add_member('wire', '标准宿主')
    service.store.set_member_token('wire', 'wire-test-token')
    service.close()
    env = dict(os.environ, BUTIAN_HOME=str(tmp_path))
    env.pop('BUTIAN_PROVIDERS_FILE', None)
    auth = {'actor_member_id': 'wire', 'token': 'wire-test-token'}
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'goal-host', 'version': '1'}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'butian_goal_submit', 'arguments': dict(auth, goal='检查真实业务', problem={'constraints': ['验收后才发布']})}},
    ]
    proc = subprocess.run([sys.executable, '-m', 'butian.cli', 'mcp'], cwd=Path(__file__).resolve().parents[1], env=env, input='\n'.join(json.dumps(x) for x in requests) + '\n', text=True, encoding='utf-8', capture_output=True, timeout=15)
    assert proc.returncode == 0, proc.stderr
    replies = {x['id']: x for x in map(json.loads, proc.stdout.splitlines())}
    names = {x['name'] for x in replies[2]['result']['tools']}
    assert {'butian_goal_submit', 'butian_problem_get', 'butian_problem_update'} <= names
    wire = replies[3]['result']
    assert not wire['isError']
    result = json.loads(wire['content'][0]['text'])
    service = ButianService(tmp_path)
    try:
        from butian.runtime.problem import ProblemModels
        assert ProblemModels(service.store).get(result['task_id'])['constraints'] == ['验收后才发布']
        assert service.store.db.execute('SELECT COUNT(*) FROM provider_sessions').fetchone()[0] == 0
    finally:
        service.close()


def test_save_revalidates_model_and_derives_definition_status(tmp_path):
    from butian.runtime.problem import ProblemModels
    service = ButianService(tmp_path)
    try:
        service.add_member('owner', '负责人')
        session = service.store.create_session('solo', 'owner', None, {})
        task = service.store.create_task(session, '目标', assignee_id='owner')
        models = ProblemModels(service.store)
        with pytest.raises(ValueError, match='invalid_problem_field:constraints'):
            models.save(task, {'goal': '目标', 'constraints': '无效列表'}, actor_id='owner', reason='输入')
        assert service.store.db.execute('SELECT COUNT(*) FROM problem_models').fetchone()[0] == 0
        saved = models.save(task, {'goal': ' 目标 ', 'acceptance_criteria': [], 'definition_status': 'defined', 'clarification_questions': []}, actor_id='owner', reason='输入')
        assert saved['goal'] == '目标'
        assert saved['definition_status'] == 'needs_clarification'
        assert saved['clarification_questions']
    finally:
        service.close()
