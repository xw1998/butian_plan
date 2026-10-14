import pytest
from test_tools import make_service, propose, execute_in_task, execution_task_id, registered_tool_proofs, MANIFEST

@pytest.fixture
def ready(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = make_service(tmp_path)
    tool = propose(service)['tool_id']
    manifest = {**MANIFEST, 'permissions': ['execute'], 'risk_level': 'low'}
    service.add_tool_version('m1', tool, 'import sys; print(sys.stdin.read())', manifest)
    for i in range(4):
        execution = execute_in_task(service, 'm1', tool, 1, 'case-' + str(i), verification_only=True)
        service.verify_tool('m1', tool, 1, 'success', independent_key=str(i), task_id=execution_task_id(service, execution['execution_id']), environment={'env': i % 2}, evidence_ids=registered_tool_proofs(service, 'm1', ['proof-' + str(i)]), execution_id=execution['execution_id'], input_hash=execution['input_hash'], expected_output=execution['expected_output'])
    assert service.tools._tool(tool)['status'] == 'active'
    contract = {'input_contract': {'type': 'text'}, 'output_contract': {'type': 'text'}, 'permissions': ['execute'], 'failure_conditions': ['step_failure'], 'steps': [{'tool_id': tool, 'version': 1}, {'tool_id': tool, 'version': 1}], 'timeout_seconds': 10, 'max_output_bytes': 65536}
    session = service.store.create_session('solo', 'm1', None, {})
    task = service.store.create_task(session, 'Workflow verification', 'm1')
    yield service, tool, contract, task
    service.close()

def test_workflow_requires_separate_real_verification(ready):
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Two steps', contract)
    identifier = flow['workflow_id']
    with pytest.raises(ValueError, match='independent_verification'):
        service.execute_tool_workflow('m1', identifier, 1, 'input', task_id=task)
    run = service.execute_tool_workflow('m1', identifier, 1, 'input', task_id=task, verification_only=True)
    assert run['status'] == 'success' and len(run['steps']) == 2
    with pytest.raises(ValueError, match='output_mismatch'):
        service.verify_tool_workflow('m1', identifier, 1, run['run_id'], evidence_ids=registered_tool_proofs(service, 'm1', ['workflow']), expected_output='wrong')
    evidence = registered_tool_proofs(service, 'm1', ['workflow'])
    verified = service.verify_tool_workflow('m1', identifier, 1, run['run_id'], evidence_ids=evidence, expected_output=run['output'])
    assert verified['status'] == 'candidate'
    session2 = service.store.create_session('solo', 'm1', None, {})
    task2 = service.store.create_task(session2, 'Independent workflow verification', 'm1')
    run2 = service.execute_tool_workflow('m1', identifier, 1, 'independent input', task_id=task2, verification_only=True)
    verified = service.verify_tool_workflow('m1', identifier, 1, run2['run_id'], evidence_ids=registered_tool_proofs(service, 'm1', ['independent-workflow']), expected_output=run2['output'])
    assert verified['status'] == 'active'
    production = service.execute_tool_workflow('m1', identifier, 1, 'new input', task_id=task)
    assert production['status'] == 'success'
    from pathlib import Path
    proof_path = service.store.db.execute('SELECT path FROM evidence WHERE evidence_id=?', (evidence[0],)).fetchone()[0]
    Path(proof_path).write_text('changed observation', encoding='utf-8')
    with pytest.raises(ValueError, match='workflow_verification_evidence_stale'):
        service.execute_tool_workflow('m1', identifier, 1, 'input', task_id=task)
    assert service.tool_workflows.get('m1', identifier, 1)['status'] == 'candidate'
    with pytest.raises(ValueError, match='tool_has_workflow_dependencies'):
        service.archive_tool('m1', tool)
    service.store.db.execute("UPDATE tool_assets SET status='quarantined' WHERE tool_id=?", (tool,))
    with pytest.raises(ValueError, match='dependency_requires_reverification'):
        service.execute_tool_workflow('m1', identifier, 1, 'new input', task_id=task, verification_only=True)
    assert service.tool_workflows.get('m1', identifier, 1)['status'] == 'candidate'

def test_workflow_permissions_versions_and_owner(ready):
    service, tool, contract, task = ready
    with pytest.raises(ValueError, match='permissions_incomplete'):
        service.create_tool_workflow('m1', 'Bad', {**contract, 'permissions': []})
    flow = service.create_tool_workflow('m1', 'First', contract)
    newer = service.create_tool_workflow('m1', 'Second', contract, workflow_id=flow['workflow_id'])
    assert newer['version'] == 2
    with pytest.raises(ValueError, match='workflow_access_denied'):
        service.tool_workflows.get('m2', flow['workflow_id'], 1)
    assert service.store.db.execute('SELECT COUNT(*) FROM tool_workflow_runs').fetchone()[0] == 0

def test_interrupted_workflow_is_unknown_without_replay(ready):
    from butian.runtime.tool_workflows import ToolWorkflows
    import json
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Interrupted', contract)
    service.store.db.execute('INSERT INTO tool_workflow_runs VALUES (?,?,?,?,?,?,?)', ('interrupted-run', flow['workflow_id'], 1, 'm1', task, 'running', json.dumps({'verification_only': False})))
    before = service.store.db.execute('SELECT COUNT(*) FROM tool_executions').fetchone()[0]
    ToolWorkflows(service.tools)
    row = service.store.db.execute('SELECT status,payload FROM tool_workflow_runs WHERE run_id=?', ('interrupted-run',)).fetchone()
    assert row['status'] == 'unknown'
    assert json.loads(row['payload'])['reason'] == 'workflow_execution_interrupted'
    assert service.store.db.execute('SELECT COUNT(*) FROM tool_executions').fetchone()[0] == before

def test_archive_checks_workflow_dependencies(ready):
    service, tool, contract, task = ready
    service.create_tool_workflow('m1', 'Dependent', contract)
    with pytest.raises(ValueError, match='tool_has_workflow_dependencies'):
        service.archive_tool('m1', tool)
    assert service.tools._tool(tool)['status'] == 'active'

def test_workflow_stops_after_uncommitted_step_audit(ready, monkeypatch):
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Audit failure', contract)
    calls = []
    def execute(*args, **kwargs):
        calls.append(args)
        return {'status': 'success', 'stdout': 'x', 'stderr': '', 'expected_output': 'x', 'audit_status': 'persistence_failed'}
    monkeypatch.setattr(service.tools, 'execute', execute)
    run = service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'input', task_id=task, verification_only=True)
    assert run['status'] == 'unknown'
    assert run['reason'] == 'workflow_step_audit_uncommitted'
    assert len(calls) == 1

def test_workflow_rejects_unknown_intermediate_dry_run(ready):
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Dry run', contract)
    with pytest.raises(ValueError, match='intermediate_output_unavailable'):
        service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'input', task_id=task, verification_only=True, dry_run=True)
    assert service.store.db.execute('SELECT COUNT(*) FROM tool_workflow_runs').fetchone()[0] == 0

def test_workflow_multibyte_budget_stops_next_step(ready, monkeypatch):
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Budget', {**contract, 'max_output_bytes': 2})
    calls = []
    def execute(*args, **kwargs):
        calls.append(kwargs)
        return {'status': 'success', 'stdout': '水', 'stderr': '', 'expected_output': '水', 'audit_status': 'committed'}
    monkeypatch.setattr(service.tools, 'execute', execute)
    run = service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'input', task_id=task, verification_only=True)
    assert run['status'] == 'failed' and run['reason'] == 'workflow_budget_exhausted'
    assert len(calls) == 1

def test_workflow_verification_rejects_missing_step_execution(ready):
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Bound execution', contract)
    run = service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'input', task_id=task, verification_only=True)
    service.store.db.execute('DELETE FROM tool_executions WHERE execution_id=?', (run['steps'][0]['execution_id'],))
    with pytest.raises(ValueError, match='workflow_step_execution_unverified'):
        service.verify_tool_workflow('m1', flow['workflow_id'], 1, run['run_id'], evidence_ids=registered_tool_proofs(service, 'm1', ['bound']), expected_output=run['output'])
    assert service.store.db.execute('SELECT COUNT(*) FROM tool_workflow_verifications').fetchone()[0] == 0

def test_workflow_retirement_releases_archive_dependency(ready):
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Retire', contract)
    with pytest.raises(ValueError, match='workflow_reason_required'):
        service.tool_workflows.retire('m1', flow['workflow_id'], 1, reason='')
    retired = service.tool_workflows.retire('m1', flow['workflow_id'], 1, reason='Replace obsolete composition')
    assert retired['status'] == 'retired'
    with pytest.raises(ValueError, match='workflow_retired'):
        service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'input', task_id=task, verification_only=True)
    assert service.archive_tool('m1', tool)['status'] == 'archived'

def test_workflow_verification_audit_fault_preserves_outer_transaction(ready):
    import sqlite3
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Atomic verification', contract)
    run = service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'input', task_id=task, verification_only=True)
    evidence = registered_tool_proofs(service, 'm1', ['atomic-workflow'])
    db = service.store.db
    db.execute("CREATE TEMP TRIGGER workflow_audit_fault BEFORE INSERT ON events WHEN NEW.event_type='workflow_verified' BEGIN SELECT RAISE(ABORT,'workflow_audit_fault'); END")
    db.execute('BEGIN')
    with pytest.raises(sqlite3.IntegrityError, match='workflow_audit_fault'):
        service.verify_tool_workflow('m1', flow['workflow_id'], 1, run['run_id'], evidence_ids=evidence, expected_output=run['output'])
    assert db.in_transaction
    assert db.execute('SELECT COUNT(*) FROM tool_workflow_verifications').fetchone()[0] == 0
    assert service.tool_workflows.get('m1', flow['workflow_id'], 1)['status'] == 'candidate'
    db.rollback()

@pytest.mark.parametrize('same', ['task', 'evidence'])
def test_workflow_repeated_task_or_proof_cannot_activate(ready, same):
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Independent samples', contract)
    proof = registered_tool_proofs(service, 'm1', ['independence-first'])
    first = service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'first', task_id=task, verification_only=True)
    service.verify_tool_workflow('m1', flow['workflow_id'], 1, first['run_id'], evidence_ids=proof, expected_output=first['output'])
    second_task = task
    if same == 'evidence':
        session = service.store.create_session('solo', 'm1', None, {})
        second_task = service.store.create_task(session, 'Another task', 'm1')
    second_proof = proof if same == 'evidence' else registered_tool_proofs(service, 'm1', ['independence-second'])
    second = service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'second', task_id=second_task, verification_only=True)
    result = service.verify_tool_workflow('m1', flow['workflow_id'], 1, second['run_id'], evidence_ids=second_proof, expected_output=second['output'])
    assert result['status'] == 'candidate'
    with pytest.raises(ValueError, match='independent_verification'):
        service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'production', task_id=task)


def test_production_rechecks_verified_step_execution(ready):
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Recheck history', contract)
    runs = []
    for index in range(2):
        session = service.store.create_session('solo', 'm1', None, {})
        sample_task = service.store.create_task(session, 'Historical sample ' + str(index), 'm1')
        run = service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'sample-' + str(index), task_id=sample_task, verification_only=True)
        service.verify_tool_workflow('m1', flow['workflow_id'], 1, run['run_id'], evidence_ids=registered_tool_proofs(service, 'm1', ['historical-' + str(index)]), expected_output=run['output'])
        runs.append(run)
    assert service.tool_workflows.get('m1', flow['workflow_id'], 1)['status'] == 'active'
    service.store.db.execute("UPDATE tool_executions SET status='unknown' WHERE execution_id=?", (runs[0]['steps'][0]['execution_id'],))
    before = service.store.db.execute('SELECT COUNT(*) FROM tool_workflow_runs').fetchone()[0]
    with pytest.raises(ValueError, match='workflow_verification_evidence_stale'):
        service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'production', task_id=task)
    assert service.tool_workflows.get('m1', flow['workflow_id'], 1)['status'] == 'candidate'
    assert service.store.db.execute('SELECT COUNT(*) FROM tool_workflow_runs').fetchone()[0] == before


def test_workflow_creation_reserves_writer_before_dependency_read(ready, monkeypatch):
    import sqlite3
    service, tool, contract, task = ready
    competitor = sqlite3.connect(service.store.path, isolation_level=None, timeout=0)
    original = service.tools._tool
    blocked = []
    def observe(identifier):
        if identifier == tool:
            try:
                competitor.execute("UPDATE tool_assets SET status='archived' WHERE tool_id=?", (tool,))
            except sqlite3.OperationalError as exc:
                assert 'locked' in str(exc)
                blocked.append(True)
        return original(identifier)
    monkeypatch.setattr(service.tools, '_tool', observe)
    try:
        flow = service.create_tool_workflow('m1', 'Concurrent admission', contract)
        assert flow['status'] == 'candidate' and blocked
        assert original(tool)['status'] == 'active'
    finally:
        competitor.close()


def test_second_runtime_does_not_recover_live_workflow(ready, monkeypatch):
    import threading
    from butian.runtime.tool_workflows import ToolWorkflows
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Live workflow', contract)
    entered, release = threading.Event(), threading.Event()
    original = service.tools.execute
    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(service.tools, 'execute', pause)
    results, errors = [], []
    def worker():
        try:
            results.append(service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'live', task_id=task, verification_only=True))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5)
        ToolWorkflows(service.tools)
        row = service.store.db.execute('SELECT status FROM tool_workflow_runs WHERE workflow_id=?', (flow['workflow_id'],)).fetchone()
        assert row['status'] == 'running'
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive() and not errors
    assert results[0]['status'] == 'success'
    assert service.store.db.execute("SELECT COUNT(*) FROM events WHERE event_type='workflow_execution_recovered' AND aggregate_id=?", (flow['workflow_id'],)).fetchone()[0] == 0


def test_workflow_lock_is_cross_process_and_releases_on_exit(ready):
    import subprocess
    import sys
    from pathlib import Path
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Process lock', contract)
    workflows = service.tool_workflows
    handle = workflows._execution_lock(flow['workflow_id'], 1)
    assert handle is not None
    code = "from types import SimpleNamespace; from pathlib import Path; import sys; from butian.runtime.tool_workflows import ToolWorkflows; w=ToolWorkflows.__new__(ToolWorkflows); w.tools=SimpleNamespace(home=Path(sys.argv[1])); h=w._execution_lock(sys.argv[2],1); print('busy' if h is None else 'acquired'); h.close() if h else None"
    try:
        result = subprocess.run([sys.executable, '-c', code, str(service.home), flow['workflow_id']], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10, check=True)
        assert result.stdout.strip() == 'busy'
    finally:
        handle.close()
    result = subprocess.run([sys.executable, '-c', code, str(service.home), flow['workflow_id']], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10, check=True)
    assert result.stdout.strip() == 'acquired'
    acquired = workflows._execution_lock(flow['workflow_id'], 1)
    assert acquired is not None
    acquired.close()


def test_workflow_admission_rechecks_proofs_at_running_intent(ready, monkeypatch):
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Admission race', contract)
    runs = []
    for index in range(2):
        session = service.store.create_session('solo', 'm1', None, {})
        sample_task = service.store.create_task(session, 'Sample ' + str(index), 'm1')
        run = service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'sample-' + str(index), task_id=sample_task, verification_only=True)
        service.verify_tool_workflow('m1', flow['workflow_id'], 1, run['run_id'], evidence_ids=registered_tool_proofs(service, 'm1', ['race-' + str(index)]), expected_output=run['output'])
        runs.append(run)
    original = service.tools.assert_task_authorization
    def invalidate(*args, **kwargs):
        result = original(*args, **kwargs)
        service.store.db.execute("UPDATE tool_executions SET status='unknown' WHERE execution_id=?", (runs[0]['steps'][0]['execution_id'],))
        return result
    monkeypatch.setattr(service.tools, 'assert_task_authorization', invalidate)
    before = service.store.db.execute('SELECT COUNT(*) FROM tool_workflow_runs').fetchone()[0]
    with pytest.raises(ValueError, match='workflow_verification_evidence_stale'):
        service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'production', task_id=task)
    assert service.store.db.execute('SELECT COUNT(*) FROM tool_workflow_runs').fetchone()[0] == before
    assert service.tool_workflows.get('m1', flow['workflow_id'], 1)['status'] == 'candidate'


def test_dependency_change_at_running_intent_commits_demotion(ready, monkeypatch):
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Dependency admission race', contract)
    service.store.db.execute("UPDATE tool_workflows SET status='active' WHERE workflow_id=?", (flow['workflow_id'],))
    monkeypatch.setattr(service.tool_workflows, '_independent_proofs', lambda *args: 2)
    original = service.tool_workflows._check_dependencies
    calls = []
    def change(workflow):
        calls.append(1)
        if len(calls) == 2:
            service.store.db.execute("UPDATE tool_assets SET status='quarantined' WHERE tool_id=?", (tool,))
        return original(workflow)
    monkeypatch.setattr(service.tool_workflows, '_check_dependencies', change)
    with pytest.raises(ValueError, match='workflow_dependency_requires_reverification'):
        service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'production', task_id=task)
    assert service.tool_workflows.get('m1', flow['workflow_id'], 1)['status'] == 'candidate'
    assert service.store.db.execute('SELECT COUNT(*) FROM tool_workflow_runs').fetchone()[0] == 0


def test_dependency_change_during_verification_commits_demotion(ready, monkeypatch):
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Verification dependency race', contract)
    run = service.execute_tool_workflow('m1', flow['workflow_id'], 1, 'sample', task_id=task, verification_only=True)
    evidence = registered_tool_proofs(service, 'm1', ['verification-dependency-race'])
    service.store.db.execute("UPDATE tool_workflows SET status='active' WHERE workflow_id=?", (flow['workflow_id'],))
    original = service.tool_workflows._check_dependencies
    calls = []
    def change(workflow):
        calls.append(1)
        if len(calls) == 2:
            service.store.db.execute("UPDATE tool_assets SET status='quarantined' WHERE tool_id=?", (tool,))
        return original(workflow)
    monkeypatch.setattr(service.tool_workflows, '_check_dependencies', change)
    with pytest.raises(ValueError, match='workflow_dependency_requires_reverification'):
        service.verify_tool_workflow('m1', flow['workflow_id'], 1, run['run_id'], evidence_ids=evidence, expected_output=run['output'])
    assert service.tool_workflows.get('m1', flow['workflow_id'], 1)['status'] == 'candidate'
    assert service.store.db.execute('SELECT COUNT(*) FROM tool_workflow_verifications').fetchone()[0] == 0


def test_repeated_verification_rechecks_other_sample(ready):
    service, tool, contract, task = ready
    flow = service.create_tool_workflow('m1', 'Repeated proof refresh', contract)
    samples = []
    for index in range(2):
        session = service.store.create_session('solo', 'm1', None, {})
        sample_task = service.store.create_task(session, 'Sample ' + str(index), 'm1')
        run = service.execute_tool_workflow('m1', flow['workflow_id'], 1, str(index), task_id=sample_task, verification_only=True)
        evidence = registered_tool_proofs(service, 'm1', ['repeat-proof-' + str(index)])
        service.verify_tool_workflow('m1', flow['workflow_id'], 1, run['run_id'], evidence_ids=evidence, expected_output=run['output'])
        samples.append((run, evidence))
    service.store.db.execute("UPDATE tool_executions SET status='unknown' WHERE execution_id=?", (samples[0][0]['steps'][0]['execution_id'],))
    run, evidence = samples[1]
    repeated = service.verify_tool_workflow('m1', flow['workflow_id'], 1, run['run_id'], evidence_ids=evidence, expected_output=run['output'])
    assert repeated['status'] == 'candidate'
    assert service.store.db.execute('SELECT COUNT(*) FROM tool_workflow_verifications').fetchone()[0] == 2
