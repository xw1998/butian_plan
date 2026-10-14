import sqlite3
import pytest
from butian.runtime.service import ButianService

@pytest.mark.parametrize('outer', [False, True])
def test_task_transition_event_failure_is_atomic(tmp_path, monkeypatch, outer):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('owner', 'Owner')
        session = service.create_session('solo', current_member_id='owner')
        task = service.store.create_task(session['session_id'], 'Original', 'owner')
        db = service.store.db
        db.execute("CREATE TEMP TRIGGER fail_transition_event BEFORE INSERT ON events WHEN NEW.event_type='task_transition' BEGIN SELECT RAISE(ABORT, 'transition_event_fault'); END")
        if outer:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE tasks SET title=? WHERE task_id=?', ('Caller pending', task))
        before = dict(db.execute('SELECT * FROM tasks WHERE task_id=?', (task,)).fetchone())
        operations = db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match='transition_event_fault'):
            service.store.transition_task(task, 1, 'ready', 'fault-operation')
        assert dict(db.execute('SELECT * FROM tasks WHERE task_id=?', (task,)).fetchone()) == before
        assert db.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == operations
        assert db.in_transaction == outer
        db.execute('DROP TRIGGER fail_transition_event')
        service.store.transition_task(task, 1, 'ready', 'valid-operation')
        assert db.in_transaction == outer
        if outer:
            db.rollback()
            row = db.execute('SELECT title,status,revision FROM tasks WHERE task_id=?', (task,)).fetchone()
            assert tuple(row) == ('Original', 'todo', 1)
    finally:
        if service.store.db.in_transaction:
            service.store.db.rollback()
        service.close()

@pytest.mark.parametrize('conflict', ['other_task', 'other_target', 'other_revision'])
def test_transition_idempotency_key_rejects_different_request(tmp_path, conflict):
    from butian.runtime.store import Store
    store = Store(tmp_path / 'team.db')
    try:
        store.add_member('owner', 'Owner')
        session = store.create_session('solo', 'owner', None, {})
        first = store.create_task(session, 'First', 'owner')
        second = store.create_task(session, 'Second', 'owner')
        store.transition_task(first, 1, 'ready', 'bound-operation')
        store.transition_task(first, 1, 'ready', 'bound-operation')
        task = second if conflict == 'other_task' else first
        revision = 2 if conflict == 'other_revision' else 1
        target = 'cancelled' if conflict == 'other_target' else 'ready'
        with pytest.raises(ValueError, match='operation_request_conflict'):
            store.transition_task(task, revision, target, 'bound-operation')
        assert store.db.execute('SELECT status FROM tasks WHERE task_id=?', (second,)).fetchone()[0] == 'todo'
    finally:
        store.close()

@pytest.mark.parametrize('changed', ['payload', 'question_record', 'rework_count'])
def test_transition_idempotency_binds_entire_request(tmp_path, changed):
    from butian.runtime.store import Store
    store = Store(tmp_path / 'team.db')
    try:
        store.add_member('owner', 'Owner')
        session = store.create_session('solo', 'owner', None, {})
        task = store.create_task(session, 'First', 'owner')
        store.transition_task(task, 1, 'ready', 'full-request', payload={'notes': 'original'})
        store.transition_task(task, 1, 'ready', 'full-request', payload={'notes': 'original'})
        kwargs = {'payload': {'notes': 'original'}}
        if changed == 'payload':
            kwargs['payload'] = {'notes': 'changed'}
        elif changed == 'question_record':
            kwargs['question_record'] = ('question', task, 1, 'changed')
        else:
            kwargs['rework_count'] = 7
        with pytest.raises(ValueError, match='operation_request_conflict'):
            store.transition_task(task, 1, 'ready', 'full-request', **kwargs)
    finally:
        store.close()

def test_legacy_transition_without_request_hash_fails_closed(tmp_path):
    import json
    from butian.runtime.store import Store
    store = Store(tmp_path / 'team.db')
    try:
        store.add_member('owner', 'Owner')
        session = store.create_session('solo', 'owner', None, {})
        task = store.create_task(session, 'Legacy', 'owner')
        store.transition_task(task, 1, 'ready', 'legacy-operation', payload={'notes': 'original'})
        event = store.db.execute('SELECT payload FROM events WHERE idempotency_key=?', ('legacy-operation',)).fetchone()
        payload = json.loads(event['payload'])
        payload.pop('request_sha256')
        store.db.execute('UPDATE events SET payload=? WHERE idempotency_key=?', (json.dumps(payload), 'legacy-operation'))
        before = dict(store.db.execute('SELECT * FROM tasks WHERE task_id=?', (task,)).fetchone())
        with pytest.raises(ValueError, match='operation_request_conflict'):
            store.transition_task(task, 1, 'ready', 'legacy-operation', payload={'notes': 'original'})
        assert dict(store.db.execute('SELECT * FROM tasks WHERE task_id=?', (task,)).fetchone()) == before
    finally:
        store.close()
