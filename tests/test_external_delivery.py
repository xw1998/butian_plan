import pytest
from butian.runtime.service import ButianService
from butian.mcp_server import call_tool


def setup_host(service):
    service.add_member('owner', '执行者')
    service.add_member('other', '无关成员')
    service.store.set_member_token('owner', 'delivery-test-token')
    service.store.set_member_token('other', 'other-delivery-token')
    return {'actor_member_id': 'owner', 'token': 'delivery-test-token'}


def test_host_delivery_evidence_review_restart_and_idempotency(tmp_path):
    service = ButianService(tmp_path)
    auth = setup_host(service)
    try:
        goal = call_tool(service, 'butian_goal_submit', dict(auth, goal='测量误差', problem={'constraints': ['不得发布'], 'acceptance_criteria': ['误差小于1%']}))
        package = call_tool(service, 'butian_execution_export', dict(auth, task_id=goal['task_id']))
        assert '不得发布' in package['context'] and '误差小于1%' in package['context']
        proof = tmp_path / 'measurement.txt'
        proof.write_text('实测误差0.5%，测量原始记录', encoding='utf-8')
        evidence = service.record_evidence(str(proof), owner_member_id='owner', actor_member_id='owner')
        args = dict(auth, task_id=goal['task_id'], expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id='measurement-1', output='实测误差0.5%', evidence_ids=[evidence], work_done=['测量及计算误差'], work_not_done=[], assumptions=[], questions_for_user=[], confidence=0.9)
        delivered = call_tool(service, 'butian_execution_submit', args)
        assert delivered['status'] == 'review'
        assert call_tool(service, 'butian_execution_submit', args) == delivered
        with pytest.raises(ValueError, match='submission_id_conflict'):
            call_tool(service, 'butian_execution_submit', dict(args, output='其他结果'))
        with pytest.raises(ValueError, match='task_access_denied'):
            call_tool(service, 'butian_execution_export', {'actor_member_id': 'other', 'token': 'other-delivery-token', 'task_id': goal['task_id']})
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        assert call_tool(service, 'butian_execution_submit', args) == delivered
        result = service.review_task(goal['task_id'], delivered['revision'], 'approve', reviewer_member_id='owner', actor_member_id='owner')
        assert result['status'] == 'done'
        assert service.store.db.execute('SELECT COUNT(*) FROM provider_sessions').fetchone()[0] == 0
    finally:
        service.close()


def test_manual_production_delivery_without_streaming_unknown(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('human', '人工执行者', provider_id='manual', model_id='human')
        goal = service.submit_goal('测量误差', member_id='human', problem={'acceptance_criteria': ['误差小于1%']})
        package = service.export_execution(goal['task_id'], actor_member_id='human')
        proof = tmp_path / 'manual-measurement.txt'
        proof.write_text('误差0.4%', encoding='utf-8')
        evidence = service.record_evidence(str(proof), owner_member_id='human', actor_member_id='human')
        delivered = service.submit_execution(goal['task_id'], expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id='manual-1', output='误差0.4%', evidence_ids=[evidence], actor_member_id='human')
        assert delivered['status'] == 'review'
        assert service.store.provider_session(goal['execution']['provider_session_id'])['status'] == 'completed'
        assert service.review_task(goal['task_id'], delivered['revision'], 'approve', reviewer_member_id='human', actor_member_id='human')['status'] == 'done'
    finally:
        service.close()


def test_correction_rejects_old_delivery_and_invalid_evidence(tmp_path):
    service = ButianService(tmp_path)
    auth = setup_host(service)
    try:
        goal = call_tool(service, 'butian_goal_submit', dict(auth, goal='测量', problem={'acceptance_criteria': ['留存报告']}))
        package = service.export_execution(goal['task_id'], actor_member_id='owner')
        service.update_problem(goal['task_id'], 1, {'constraints': ['采用新样本']}, actor_member_id='owner', reason='纠正')
        with pytest.raises(ValueError, match='revision_conflict'):
            service.submit_execution(goal['task_id'], expected_revision=package['task_revision'], problem_revision=1, submission_id='stale', output='旧报告', evidence_ids=['missing'], actor_member_id='owner')
        fresh = service.export_execution(goal['task_id'], actor_member_id='owner')
        with pytest.raises(ValueError):
            service.submit_execution(goal['task_id'], expected_revision=fresh['task_revision'], problem_revision=2, submission_id='invalid', output='报告', evidence_ids=['missing'], actor_member_id='owner')
        assert service.store.db.execute('SELECT status FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone()[0] == 'running'
    finally:
        service.close()


def test_delivery_fault_rolls_back_provider_task_and_handoff(tmp_path, monkeypatch):
    service = ButianService(tmp_path)
    try:
        service.add_member('human', '人工', provider_id='manual', model_id='human')
        goal = service.submit_goal('提交报告', member_id='human', problem={'acceptance_criteria': ['保留原始记录']})
        package = service.export_execution(goal['task_id'], actor_member_id='human')
        proof = tmp_path / 'fault-proof.txt'
        proof.write_text('原始记录', encoding='utf-8')
        evidence = service.record_evidence(str(proof), owner_member_id='human', actor_member_id='human')
        original = service.store.append_event
        def failing_event(*args, **kwargs):
            if args[1] == 'external_execution_submitted':
                raise RuntimeError('injected_delivery_fault')
            return original(*args, **kwargs)
        monkeypatch.setattr(service.store, 'append_event', failing_event)
        with pytest.raises(RuntimeError, match='injected_delivery_fault'):
            service.submit_execution(goal['task_id'], expected_revision=package['task_revision'], problem_revision=1, submission_id='fault', output='报告', evidence_ids=[evidence], actor_member_id='human')
        assert service.store.provider_session(goal['execution']['provider_session_id'])['status'] == 'running'
        task = service.store.db.execute('SELECT status,revision FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone()
        assert task['status'] == 'running' and task['revision'] == package['task_revision']
        assert service.store.db.execute('SELECT COUNT(*) FROM handoffs WHERE task_id=?', (goal['task_id'],)).fetchone()[0] == 0
        assert service.store.db.execute('SELECT COUNT(*) FROM provider_outputs').fetchone()[0] == 0
        table = service.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_submissions'").fetchone()
        assert not table or service.store.db.execute('SELECT COUNT(*) FROM external_submissions').fetchone()[0] == 0
    finally:
        service.close()


def test_partial_delivery_cannot_approve(tmp_path):
    service = ButianService(tmp_path)
    auth = setup_host(service)
    try:
        goal = call_tool(service, 'butian_goal_submit', dict(auth, goal='测量', problem={'acceptance_criteria': ['留存报告']}))
        package = service.export_execution(goal['task_id'], actor_member_id='owner')
        proof = tmp_path / 'partial-proof.txt'
        proof.write_text('只完成取样', encoding='utf-8')
        evidence = service.record_evidence(str(proof), owner_member_id='owner', actor_member_id='owner')
        result = service.submit_execution(goal['task_id'], expected_revision=package['task_revision'], problem_revision=1, submission_id='partial', output='取样完成', evidence_ids=[evidence], actor_member_id='owner', work_not_done=['尚未测量'])
        with pytest.raises(ValueError, match='handoff_required_for_done'):
            service.review_task(goal['task_id'], result['revision'], 'approve', reviewer_member_id='owner', actor_member_id='owner')
    finally:
        service.close()


def test_concurrent_independent_runtime_submission_is_exactly_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    service = ButianService(tmp_path)
    auth = setup_host(service)
    goal = call_tool(service, 'butian_goal_submit', dict(auth, goal='测量', problem={'acceptance_criteria': ['留存报告']}))
    package = service.export_execution(goal['task_id'], actor_member_id='owner')
    proof = tmp_path / 'concurrent-proof.txt'
    proof.write_text('报告', encoding='utf-8')
    evidence = service.record_evidence(str(proof), owner_member_id='owner', actor_member_id='owner')
    service.close()
    services = [ButianService(tmp_path), ButianService(tmp_path)]
    args = dict(expected_revision=package['task_revision'], problem_revision=1, submission_id='first', output='报告', evidence_ids=[evidence], actor_member_id='owner')
    try:
        import threading
        barrier = threading.Barrier(2)
        def deliver(svc):
            barrier.wait(timeout=5)
            return svc.submit_execution(goal['task_id'], **args)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(deliver, services))
        assert results[0] == results[1]
        assert services[0].store.db.execute('SELECT COUNT(*) FROM external_submissions').fetchone()[0] == 1
    finally:
        for svc in services:
            svc.close()


def test_http_host_delivery_authentication_and_review(tmp_path):
    import json
    import threading
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from pathlib import Path
    from butian.runtime import http_api
    service = ButianService(tmp_path)
    setup_host(service)
    class Handler(http_api.ApiHandler):
        pass
    Handler.service = service
    Handler.experiments = None
    Handler.web_root = Path(__file__).resolve().parents[1] / 'web'
    server = http_api.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_address[1])
    def post(path, data, token='delivery-test-token'):
        req = Request(base + path, data=json.dumps(data).encode(), headers={'Content-Type': 'application/json', 'X-Butian-Member': 'owner', 'X-Butian-Token': token}, method='POST')
        try:
            with urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())
    try:
        status, response = post('/api/goals/host-submit', {'goal': '测量', 'problem': {'acceptance_criteria': ['留存报告']}})
        assert status == 200
        task_id = response['data']['task_id']
        status, response = post('/api/executions/export', {'task_id': task_id}, token='wrong')
        assert status >= 400
        status, response = post('/api/executions/export', {'task_id': task_id})
        assert status == 200
        package = response['data']
        proof = tmp_path / 'http-report.txt'
        proof.write_text('报告', encoding='utf-8')
        evidence = service.record_evidence(str(proof), owner_member_id='owner', actor_member_id='owner')
        status, response = post('/api/executions/submit', {'task_id': task_id, 'expected_revision': package['task_revision'], 'problem_revision': package['problem_revision'], 'submission_id': 'http-report', 'output': '完成报告', 'evidence_ids': [evidence]})
        assert status == 200 and response['data']['status'] == 'review'
        result = service.review_task(task_id, response['data']['revision'], 'approve', reviewer_member_id='owner', actor_member_id='owner')
        assert result['status'] == 'done'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        service.close()

def test_manual_rework_can_deliver_again(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('human', '人工执行者', provider_id='manual', model_id='human')
        goal = service.submit_goal('测量误差', member_id='human', problem={'acceptance_criteria': ['误差小于1%']})
        proof = tmp_path / 'rework.txt'
        proof.write_text('第一次测量0.4%，第二次测量0.3%', encoding='utf-8')
        evidence = service.record_evidence(str(proof), owner_member_id='human', actor_member_id='human')
        def deliver(key):
            package = service.export_execution(goal['task_id'], actor_member_id='human')
            return service.submit_execution(goal['task_id'], expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id=key, output='误差0.3%', evidence_ids=[evidence], actor_member_id='human')
        first = deliver('first')
        service.review_task(goal['task_id'], first['revision'], 'rework', notes='补充复测', reviewer_member_id='human', actor_member_id='human')
        second = deliver('second')
        assert service.review_task(goal['task_id'], second['revision'], 'approve', reviewer_member_id='human', actor_member_id='human')['status'] == 'done'
    finally:
        service.close()

def test_export_context_fault_preserves_task_and_events(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        auth = setup_host(service)
        goal = call_tool(service, 'butian_goal_submit', dict(auth, goal='Atomic export', problem={'acceptance_criteria': ['Export succeeds completely or preserves task']}))
        task_id = goal['task_id']
        before = dict(service.store.db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
        events = service.store.db.execute('SELECT COUNT(*) FROM events WHERE aggregate_id=?', (task_id,)).fetchone()[0]
        def fail_context(*args, **kwargs):
            raise RuntimeError('injected_export_context_fault')
        monkeypatch.setattr(service, 'compose_context', fail_context)
        with pytest.raises(RuntimeError, match='injected_export_context_fault'):
            service.export_execution(task_id, actor_member_id='owner')
        assert dict(service.store.db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()) == before
        assert service.store.db.execute('SELECT COUNT(*) FROM events WHERE aggregate_id=?', (task_id,)).fetchone()[0] == events
    finally:
        service.close()
