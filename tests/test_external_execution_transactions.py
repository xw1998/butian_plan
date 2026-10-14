import pytest
from butian.runtime.service import ButianService

@pytest.mark.parametrize('operation', ['export', 'submit'])
@pytest.mark.parametrize('audit_fails', [False, True])
def test_host_execution_preserves_outer_transaction(tmp_path, monkeypatch, operation, audit_fails):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('owner', 'Owner', provider_id='manual', model_id='human')
        goal = service.submit_goal('Produce measured report', member_id='owner', problem={'acceptance_criteria': ['Retain measurement evidence']})
        task_id = goal['task_id']
        package = service.export_execution(task_id, actor_member_id='owner') if operation == 'submit' else None
        proof = tmp_path / 'proof.txt'
        proof.write_text('Measured result 1.25', encoding='utf-8')
        evidence = service.record_evidence(str(proof), owner_member_id='owner', actor_member_id='owner')
        db = service.store.db
        original = dict(db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE tasks SET title=? WHERE task_id=?', ('Caller pending title', task_id))
        pending = dict(db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
        def invoke():
            if operation == 'export':
                return service.export_execution(task_id, actor_member_id='owner')
            return service.submit_execution(task_id, expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id='outer', output='Measured result 1.25', evidence_ids=[evidence], actor_member_id='owner')
        if audit_fails:
            def fail(*args, **kwargs):
                raise RuntimeError('host_audit_fault')
            monkeypatch.setattr(service.store, 'append_event', fail)
            with pytest.raises(RuntimeError, match='host_audit_fault'):
                invoke()
            assert dict(db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()) == pending
        else:
            invoke()
        assert db.in_transaction
        db.rollback()
        assert dict(db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()) == original
    finally:
        if service.store.db.in_transaction:
            service.store.db.rollback()
        service.close()
