import pytest

from butian.runtime.service import ButianService
from butian.runtime.planning_execution import create, bind, finish


@pytest.mark.parametrize('operation', ['create', 'bind', 'finish'])
def test_planning_event_failure_rolls_back_record(tmp_path, monkeypatch, operation):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    service.add_member('m', 'Member')
    planning = None
    session = None
    if operation != 'create':
        planning = create(service.store, 'm', 1)
        session = service.store.create_provider_session('m', 'local-command',
            remote_session_id='planning', prompt='prompt')
    if operation == 'finish':
        bind(service.store, planning, session)
    before = [tuple(row) for row in service.store.db.execute('SELECT * FROM planning_executions')]
    def fail(*args, **kwargs):
        raise RuntimeError('audit_write_fault')
    monkeypatch.setattr(service.store, 'append_event', fail)
    try:
        with pytest.raises(RuntimeError, match='audit_write_fault'):
            if operation == 'create':
                create(service.store, 'm', 1)
            elif operation == 'bind':
                bind(service.store, planning, session)
            else:
                finish(service.store, planning, 'revoked')
        assert [tuple(row) for row in service.store.db.execute('SELECT * FROM planning_executions')] == before
        assert not service.store.db.in_transaction
    finally:
        service.close()


def test_planning_permission_detects_persisted_prompt_change(tmp_path, monkeypatch):
    import hashlib
    from butian.runtime.planning_execution import active
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    service.add_member('m', 'Member')
    try:
        planning = create(service.store, 'm', 1)
        session = service.store.create_provider_session('m', 'local-command', remote_session_id='planning', prompt='original')
        bind(service.store, planning, session)
        digest = hashlib.sha256(b'original').hexdigest()
        assert active(service.store, planning, 'm', session, digest)
        service.store.db.execute('UPDATE provider_sessions SET prompt=? WHERE provider_session_id=?', ('changed', session))
        assert not active(service.store, planning, 'm', session, digest)
    finally:
        service.close()

def test_generated_plan_metadata_keeps_problem_revision(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    service.add_member('dispatcher', 'Dispatcher', provider_id='manual')
    service.add_member('worker', 'Worker')
    service.store.db.execute("UPDATE members SET provider_id='fixture' WHERE member_id='dispatcher'")
    session = service.store.create_provider_session('dispatcher', 'manual', remote_session_id='revision-plan', prompt='prompt')
    monkeypatch.setattr(service, 'start_provider_task', lambda *args: {'status': 'running', 'provider_session_id': session})
    monkeypatch.setattr(service.providers, 'get', lambda *args: object())
    monkeypatch.setattr(service.provider_runtime, 'consume', lambda *args: {'status': 'completed', 'text': '{"subtasks":["Verify evidence"]}'})
    try:
        plans, metadata = service._plan_team_goal('dispatcher', 'Verify evidence', None, ['worker'], {'revision': 7})
        assert metadata['problem_revision'] == 7
        assert service.store.db.execute('SELECT problem_revision FROM planning_executions').fetchone()[0] == 7
    finally:
        service.close()

def test_planning_permission_rejects_changed_provider_identity(tmp_path, monkeypatch):
    import hashlib
    from butian.runtime.planning_execution import active
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    service.add_member('m', 'Member')
    try:
        planning = create(service.store, 'm', 1)
        session = service.store.create_provider_session('m', 'local-command', remote_session_id='identity', prompt='prompt')
        bind(service.store, planning, session)
        digest = hashlib.sha256(b'prompt').hexdigest()
        assert active(service.store, planning, 'm', session, digest)
        service.store.db.execute("UPDATE provider_sessions SET provider_id='manual' WHERE provider_session_id=?", (session,))
        assert not active(service.store, planning, 'm', session, digest)
    finally:
        service.close()

def test_recovery_does_not_revoke_live_planning_owner(tmp_path, monkeypatch):
    from butian.runtime.planning_execution import recover, ownership
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    service.add_member('m', 'Member')
    try:
        with ownership(service.store) as planning_id:
            planning = create(service.store, 'm', 1, planning_id=planning_id)
            assert recover(service.store) == []
            assert service.store.db.execute('SELECT status FROM planning_executions WHERE planning_id=?', (planning,)).fetchone()[0] == 'prepared'
        assert recover(service.store) == [planning]
        assert service.store.db.execute('SELECT status FROM planning_executions WHERE planning_id=?', (planning,)).fetchone()[0] == 'unknown'
    finally:
        service.close()

def test_second_runtime_keeps_provider_planning_authorized(tmp_path, monkeypatch):
    from butian.runtime.planning_execution import active
    import hashlib
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    service.add_member('dispatcher', 'Dispatcher')
    service.add_member('worker', 'Worker')
    service.store.db.execute("UPDATE members SET provider_id='fixture' WHERE member_id='dispatcher'")
    session = service.store.create_provider_session('dispatcher', 'manual', remote_session_id='live-plan', prompt='prompt')
    monkeypatch.setattr(service, 'start_provider_task', lambda *args: {'status': 'running', 'provider_session_id': session})
    monkeypatch.setattr(service.providers, 'get', lambda *args: object())
    def consume(*args):
        planning = service.store.db.execute('SELECT planning_id FROM planning_executions').fetchone()[0]
        other = ButianService(tmp_path)
        try:
            assert other.startup_recovery['planning_executions'] == []
            assert active(service.store, planning, 'dispatcher', session, hashlib.sha256(b'prompt').hexdigest())
        finally:
            other.close()
        return {'status': 'completed', 'text': '{"subtasks":["Verify evidence"]}'}
    monkeypatch.setattr(service.provider_runtime, 'consume', consume)
    try:
        service._plan_team_goal('dispatcher', 'Verify evidence', None, ['worker'], {'revision': 2})
        assert service.store.db.execute('SELECT status FROM planning_executions').fetchone()[0] == 'completed'
    finally:
        service.close()
