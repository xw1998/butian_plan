import sqlite3

import pytest

from butian.runtime.learning import LearningStore


@pytest.mark.parametrize('limit', [True, '1', 1.5])
def test_queue_limit_requires_integer(limit):
    db = sqlite3.connect(':memory:', isolation_level=None)
    store = LearningStore(db)
    try:
        with pytest.raises(ValueError, match='invalid_learning_queue_limit'):
            store.process_learning_queue(limit=limit)
    finally:
        db.close()


def test_claim_audit_failure_rolls_back_state(tmp_path):
    db = sqlite3.connect(tmp_path / 'queue.db', isolation_level=None)
    store = LearningStore(db)
    queue_id = store.enqueue_url('https://source.example/evidence')
    db.execute("CREATE TRIGGER reject_claim BEFORE INSERT ON learning_queue_events WHEN NEW.event_type='claimed' BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
    try:
        with pytest.raises(sqlite3.IntegrityError, match='audit unavailable'):
            store._claim_queue_item(queue_id, 'worker', now=1000)
        row = db.execute('SELECT status,attempts,worker_id FROM learning_queue WHERE queue_id=?', (queue_id,)).fetchone()
        assert tuple(row) == ('pending', 0, None)
        assert not db.in_transaction
    finally:
        db.close()


def test_completion_audit_failure_preserves_owned_lease(tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / 'complete.db', isolation_level=None)
    store = LearningStore(db, clock=lambda: 1000)
    queue_id = store.enqueue_url('https://source.example/evidence')
    monkeypatch.setattr(store, 'capture_url', lambda *a, **kw: {'source_id': 'source', 'run_id': 'run'})
    db.execute("CREATE TRIGGER reject_complete BEFORE INSERT ON learning_queue_events WHEN NEW.event_type='completed' BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
    try:
        with pytest.raises(sqlite3.IntegrityError, match='audit unavailable'):
            store.process_learning_queue(worker_id='worker')
        row = db.execute('SELECT status,source_id,worker_id FROM learning_queue WHERE queue_id=?', (queue_id,)).fetchone()
        assert tuple(row) == ('running', None, 'worker')
        assert not db.in_transaction
    finally:
        db.close()


def test_renewal_audit_failure_rolls_back_lease(tmp_path):
    db = sqlite3.connect(tmp_path / 'renew.db', isolation_level=None)
    store = LearningStore(db, clock=lambda: 1000)
    queue_id = store.enqueue_url('https://source.example/evidence')
    claimed = store._claim_queue_item(queue_id, 'worker', now=1000, lease_seconds=10)
    db.execute("CREATE TRIGGER reject_renew BEFORE INSERT ON learning_queue_events WHEN NEW.event_type='lease_renewed' BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
    try:
        with pytest.raises(sqlite3.IntegrityError, match='audit unavailable'):
            store.renew_learning_queue_lease(queue_id, 'worker', claimed['lease_token'], lease_seconds=30)
        assert db.execute('SELECT lease_until FROM learning_queue WHERE queue_id=?', (queue_id,)).fetchone()[0] == 1010
        assert not db.in_transaction
    finally:
        db.close()

@pytest.mark.parametrize('limit', [True, '1', 1.5, 0, 501])
def test_queue_list_rejects_invalid_limits(limit):
    db = sqlite3.connect(':memory:', isolation_level=None)
    store = LearningStore(db)
    try:
        with pytest.raises(ValueError, match='invalid_learning_queue_limit'):
            store.list_learning_queue(limit=limit)
    finally:
        db.close()

@pytest.mark.parametrize('options', [{'timeout': True}, {'timeout': '2'}, {'timeout': float('nan')}, {'max_bytes': True}, {'max_bytes': 1.5}, {'max_bytes': '100'}])
def test_enqueue_rejects_coerced_fetch_limits(options):
    db = sqlite3.connect(':memory:', isolation_level=None)
    store = LearningStore(db)
    try:
        with pytest.raises(ValueError, match='invalid_learning_fetch_limits'):
            store.enqueue_url('https://example.org/source', **options)
    finally:
        db.close()

@pytest.mark.parametrize('kwargs', [{'timeout': True}, {'timeout': '2'}, {'timeout': float('nan')}, {'max_bytes': True}, {'max_bytes': 1.5}, {'max_bytes': '100'}])
def test_direct_capture_rejects_invalid_fetch_types(tmp_path, kwargs):
    from butian.runtime.service import ButianService
    service = ButianService(tmp_path)
    try:
        with pytest.raises(ValueError, match='invalid_learning_fetch_limits'):
            service.learning.capture_url('https://example.com/data', **kwargs)
        assert service.store.db.execute('SELECT COUNT(*) FROM learning_runs').fetchone()[0] == 0
    finally:
        service.close()
