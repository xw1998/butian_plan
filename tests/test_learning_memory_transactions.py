import sqlite3
import pytest
from butian.runtime.learning import LearningStore
from butian.runtime.memory import MemoryStore


def test_enqueue_preserves_outer_transaction():
    db = sqlite3.connect(':memory:', isolation_level=None)
    store = LearningStore(db)
    try:
        db.execute('BEGIN IMMEDIATE')
        store.enqueue_url('https://example.org/transaction')
        assert db.in_transaction
        db.rollback()
        assert db.execute('SELECT COUNT(*) FROM learning_queue').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM learning_queue_events').fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize('outer', [False, True])
def test_put_version_failure_restores_content_and_index(tmp_path, outer):
    store = MemoryStore(tmp_path / 'memory.db')
    try:
        store.put('method', 'method', 'original searchable content')
        store.db.execute("CREATE TRIGGER reject_version BEFORE INSERT ON memory_versions BEGIN SELECT RAISE(ABORT, 'version unavailable'); END")
        if outer:
            store.db.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.IntegrityError, match='version unavailable'):
            store.put('method', 'method', 'replacement content')
        assert store.get('method')['content'] == 'original searchable content'
        assert store.db.execute('SELECT content FROM memory_fts WHERE memory_id=?', ('method',)).fetchone()[0] == 'original searchable content'
        assert len(store.list_versions('method')) == 1
        assert store.db.in_transaction is outer
    finally:
        store.db.rollback()
        store.close()


@pytest.mark.parametrize('operation', ['enqueue', 'mark'])
@pytest.mark.parametrize('outer', [False, True])
def test_outbox_audit_failure_rolls_back_only_own_changes(tmp_path, operation, outer):
    from butian.runtime.store import Store
    store = Store(tmp_path / 'team.db')
    try:
        oid = store.enqueue_member_memory('first', 'worker', 'method', 'lesson', 'content', {}, [])
        before = store.db.execute('SELECT COUNT(*) FROM member_memory_outbox_events').fetchone()[0]
        store.db.execute("CREATE TRIGGER reject_outbox_event BEFORE INSERT ON member_memory_outbox_events BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        if outer:
            store.db.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.IntegrityError, match='audit unavailable'):
            if operation == 'enqueue':
                store.enqueue_member_memory('second', 'worker', 'method', 'lesson', 'new content', {}, [])
            else:
                store.mark_member_memory_outbox(oid, 'committed')
        assert store.db.execute('SELECT COUNT(*) FROM member_memory_outbox').fetchone()[0] == 1
        assert store.db.execute('SELECT status FROM member_memory_outbox WHERE outbox_id=?', (oid,)).fetchone()[0] == 'pending'
        assert store.db.execute('SELECT COUNT(*) FROM member_memory_outbox_events').fetchone()[0] == before
        assert store.db.in_transaction is outer
    finally:
        store.db.rollback()
        store.close()


@pytest.mark.parametrize('outer', [False, True])
def test_event_insert_failure_does_not_leave_committed_operation(tmp_path, outer):
    from butian.runtime.store import Store
    store = Store(tmp_path / 'events.db')
    try:
        store.db.execute("CREATE TRIGGER reject_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'event unavailable'); END")
        if outer:
            store.db.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.IntegrityError, match='event unavailable'):
            store.append_event('worker', 'budget_configured', {}, 'operation')
        assert store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 0
        assert store.db.in_transaction is outer
    finally:
        store.db.rollback()
        store.close()


@pytest.mark.parametrize('operation', ['claim', 'renew'])
def test_queue_lease_preserves_outer_transaction(operation):
    db = sqlite3.connect(':memory:', isolation_level=None)
    store = LearningStore(db, clock=lambda: 100)
    try:
        queue_id = store.enqueue_url('https://example.org/lease')
        initial = store._claim_queue_item(queue_id, 'worker', now=100) if operation == 'renew' else None
        db.execute('BEGIN IMMEDIATE')
        if operation == 'claim':
            store._claim_queue_item(queue_id, 'worker', now=100)
        else:
            store.renew_learning_queue_lease(queue_id, 'worker', initial['lease_token'])
        assert db.in_transaction
        db.rollback()
        row = db.execute('SELECT status,attempts FROM learning_queue WHERE queue_id=?', (queue_id,)).fetchone()
        assert tuple(row) == (('pending', 0) if operation == 'claim' else ('running', 1))
    finally:
        db.close()


def test_learning_initialization_preserves_outer_transaction():
    db = sqlite3.connect(':memory:', isolation_level=None)
    try:
        db.execute('CREATE TABLE caller_marker(value TEXT)')
        db.execute('BEGIN')
        db.execute("INSERT INTO caller_marker VALUES ('uncommitted')")
        LearningStore(db)
        assert db.in_transaction
        db.rollback()
        assert db.execute('SELECT COUNT(*) FROM caller_marker').fetchone()[0] == 0
    finally:
        db.close()
