import sqlite3

import pytest

from butian.runtime.execution_contract import CapabilityEvidence, ExecutionCapabilities, ExecutionObservation, ExecutionRequest, REQUIRED_CONTROL
from butian.runtime.execution_dispatch import ExecutionDispatch


class Adapter:
    def __init__(self):
        self.starts = 0
        self.lost = False
        self.instance = 'i1'

    def capabilities(self):
        return ExecutionCapabilities('owner', self.instance, {
            name: CapabilityEvidence('supported', 'self_test', 1, self.instance)
            for name in REQUIRED_CONTROL})

    def start(self, request):
        self.starts += 1
        if self.lost:
            raise ConnectionError('response lost')
        return self.inspect(request.execution_id)

    def inspect(self, execution_id):
        return ExecutionObservation(execution_id, 'owner', self.instance, 1, 'running')

    def events(self, execution_id, after_sequence=-1):
        values = [ExecutionObservation(execution_id, 'owner', self.instance, 0, 'accepted'),
                  self.inspect(execution_id)]
        return [value for value in values if value.sequence > after_sequence]


def request():
    return ExecutionRequest('e1', 'a' * 64, ('python',), '.', b'', 1, 1024, {})


def test_lost_response_reconnect_never_restarts(tmp_path):
    path = tmp_path / 'dispatch.db'
    adapter = Adapter()
    adapter.lost = True
    db = sqlite3.connect(path, isolation_level=None)
    with pytest.raises(ConnectionError):
        ExecutionDispatch(db, adapter).start(request())
    db.close()
    db = sqlite3.connect(path, isolation_level=None)
    try:
        assert ExecutionDispatch(db, adapter).start(request()).state == 'running'
        assert adapter.starts == 1
    finally:
        db.close()


def test_caller_transaction_cannot_send_uncommitted_intent():
    db = sqlite3.connect(':memory:', isolation_level=None)
    adapter = Adapter()
    dispatch = ExecutionDispatch(db, adapter)
    db.execute('BEGIN')
    with pytest.raises(ValueError, match='execution_dispatch_requires_committed_transaction'):
        dispatch.start(request())
    assert db.in_transaction
    assert adapter.starts == 0
    db.rollback()


def test_owner_change_prevents_reconnection_and_fallback():
    db = sqlite3.connect(':memory:', isolation_level=None)
    adapter = Adapter()
    dispatch = ExecutionDispatch(db, adapter)
    dispatch.start(request())
    adapter.instance = 'i2'
    with pytest.raises(ValueError, match='execution_dispatch_owner_changed'):
        dispatch.reconcile('e1')
    assert adapter.starts == 1


def test_intent_storage_failure_prevents_spawn():
    db = sqlite3.connect(':memory:', isolation_level=None)
    adapter = Adapter()
    dispatch = ExecutionDispatch(db, adapter)
    db.execute("CREATE TRIGGER reject_dispatch BEFORE INSERT ON execution_dispatch BEGIN SELECT RAISE(ABORT,'storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        dispatch.start(request())
    assert adapter.starts == 0

@pytest.mark.parametrize('changes', [{'metadata': {'stdout': 'changed'}}, {'reason': 'changed'}, {'exit_code': 12}])
def test_same_sequence_changed_evidence_is_rejected(changes):
    from dataclasses import replace
    db = sqlite3.connect(':memory:', isolation_level=None)
    dispatch = ExecutionDispatch(db, Adapter())
    original = dispatch.start(request())
    with pytest.raises(ValueError, match='execution_dispatch_event_conflict'):
        dispatch.record(replace(original, **changes))
    assert dispatch.reconcile('e1') == original
    db.close()

def test_old_dispatch_table_migrates_without_losing_intent():
    db = sqlite3.connect(':memory:', isolation_level=None)
    db.execute('CREATE TABLE execution_dispatch (execution_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, executor_id TEXT NOT NULL, instance_id TEXT NOT NULL, delivery_state TEXT NOT NULL, last_sequence INTEGER NOT NULL, last_state TEXT NOT NULL, updated_at REAL NOT NULL)')
    db.execute("INSERT INTO execution_dispatch VALUES ('e1', ?, 'owner', 'i1', 'unconfirmed', 1, 'running', 0)", ('a' * 64,))
    adapter = Adapter()
    dispatch = ExecutionDispatch(db, adapter)
    assert dispatch.reconcile('e1').state == 'running'
    assert adapter.starts == 0
    assert 'last_observation' in {row[1] for row in db.execute('PRAGMA table_info(execution_dispatch)')}
    db.close()


def test_new_sequence_cannot_regress_running_to_accepted():
    db = sqlite3.connect(':memory:', isolation_level=None)
    dispatch = ExecutionDispatch(db, Adapter())
    dispatch.start(request())
    with pytest.raises(ValueError, match='execution_dispatch_state_regression'):
        dispatch.record(ExecutionObservation('e1', 'owner', 'i1', 2, 'accepted'))
    assert dispatch.reconcile('e1').state == 'running'
    db.close()


def test_reconcile_fills_event_gap_in_order():
    class History(Adapter):
        def events(self, execution_id, after_sequence=0):
            if after_sequence == -1:
                return super().events(execution_id, after_sequence)
            assert after_sequence == 1
            return [ExecutionObservation('e1', 'owner', 'i1', 2, 'cancelling', reason='actor'),
                    ExecutionObservation('e1', 'owner', 'i1', 3, 'cancelled', reason='actor', tree_exit_confirmed=True)]
    db = sqlite3.connect(':memory:', isolation_level=None)
    adapter = History()
    dispatch = ExecutionDispatch(db, adapter)
    dispatch.start(request())
    adapter.inspect = lambda _: ExecutionObservation('e1', 'owner', 'i1', 3, 'cancelled', reason='actor', tree_exit_confirmed=True)
    assert dispatch.reconcile('e1').state == 'cancelled'
    assert db.execute('SELECT count(*) FROM execution_dispatch_events').fetchone()[0] == 4
    db.close()


def test_reconcile_rejects_missing_intermediate_event():
    db = sqlite3.connect(':memory:', isolation_level=None)
    adapter = Adapter()
    dispatch = ExecutionDispatch(db, adapter)
    dispatch.start(request())
    adapter.inspect = lambda _: ExecutionObservation('e1', 'owner', 'i1', 3, 'success', exit_code=0, tree_exit_confirmed=True)
    adapter.events = lambda *args: [adapter.inspect('e1')]
    with pytest.raises(ValueError, match='execution_dispatch_event_sequence_gap'):
        dispatch.reconcile('e1')
    assert db.execute('SELECT last_sequence FROM execution_dispatch').fetchone()[0] == 1
    db.close()


def test_reconcile_conflicting_snapshot_does_not_commit_partial_history():
    db = sqlite3.connect(':memory:', isolation_level=None)
    adapter = Adapter()
    dispatch = ExecutionDispatch(db, adapter)
    dispatch.start(request())
    adapter.inspect = lambda _: ExecutionObservation('e1', 'owner', 'i1', 3, 'cancelled', reason='different', tree_exit_confirmed=True)
    adapter.events = lambda *args: [
        ExecutionObservation('e1', 'owner', 'i1', 2, 'cancelling', reason='actor'),
        ExecutionObservation('e1', 'owner', 'i1', 3, 'cancelled', reason='actor', tree_exit_confirmed=True)]
    with pytest.raises(ValueError, match='execution_dispatch_event_conflict'):
        dispatch.reconcile('e1')
    assert db.execute('SELECT last_sequence FROM execution_dispatch').fetchone()[0] == 1
    assert db.execute('SELECT count(*) FROM execution_dispatch_events').fetchone()[0] == 2
    db.close()


def test_cancel_cannot_skip_intermediate_events():
    db = sqlite3.connect(':memory:', isolation_level=None)
    adapter = Adapter()
    dispatch = ExecutionDispatch(db, adapter)
    dispatch.start(request())
    adapter.cancel = lambda *args, **kwargs: ExecutionObservation('e1', 'owner', 'i1', 3, 'cancelled', tree_exit_confirmed=True)
    adapter.events = lambda *args: [adapter.cancel('e1')]
    with pytest.raises(ValueError, match='execution_dispatch_event_sequence_gap'):
        dispatch.cancel('e1', reason='actor')
    assert db.execute('SELECT last_sequence FROM execution_dispatch').fetchone()[0] == 1
    db.close()


def test_first_observation_fetches_accepted_event():
    db = sqlite3.connect(':memory:', isolation_level=None)
    adapter = Adapter()
    calls = []
    def events(execution_id, after_sequence):
        calls.append(after_sequence)
        return [ExecutionObservation('e1', 'owner', 'i1', 0, 'accepted'), adapter.inspect(execution_id)]
    adapter.events = events
    dispatch = ExecutionDispatch(db, adapter)
    dispatch.start(request())
    assert calls == [-1]
    assert db.execute('SELECT sequence FROM execution_dispatch_events ORDER BY sequence').fetchall() == [(0,), (1,)]
    db.close()


def test_direct_record_rejects_sequence_gap():
    db = sqlite3.connect(':memory:', isolation_level=None)
    dispatch = ExecutionDispatch(db, Adapter())
    dispatch.start(request())
    with pytest.raises(ValueError, match='execution_dispatch_event_sequence_gap'):
        dispatch.record(ExecutionObservation('e1', 'owner', 'i1', 3, 'cancelled', tree_exit_confirmed=True))
    assert db.execute('SELECT last_sequence FROM execution_dispatch').fetchone()[0] == 1
    db.close()


def test_history_ahead_of_inspect_still_verifies_inspect_snapshot():
    class Ahead(Adapter):
        def events(self, execution_id, after_sequence=-1):
            if after_sequence == -1:
                return super().events(execution_id, after_sequence)
            return [ExecutionObservation('e1', 'owner', 'i1', 1, 'running'),
                    ExecutionObservation('e1', 'owner', 'i1', 2, 'cancelling', reason='actual'),
                    ExecutionObservation('e1', 'owner', 'i1', 3, 'cancelled', reason='actual', tree_exit_confirmed=True)]
    db = sqlite3.connect(':memory:', isolation_level=None)
    adapter = Ahead()
    dispatch = ExecutionDispatch(db, adapter)
    dispatch.start(request())
    adapter.inspect = lambda _: ExecutionObservation('e1', 'owner', 'i1', 2, 'cancelling', reason='conflicting')
    # Force recovery of seq2 even though inspect itself is only one step ahead.
    db.execute('DELETE FROM execution_dispatch_events WHERE sequence=1')
    db.execute('UPDATE execution_dispatch SET last_sequence=0,last_state="accepted",last_observation=NULL')
    with pytest.raises(ValueError, match='execution_dispatch_event_conflict'):
        dispatch.reconcile('e1')
    assert db.execute('SELECT last_sequence FROM execution_dispatch').fetchone()[0] == 0
    db.close()


def test_independent_connections_never_start_same_execution_twice(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'shared-dispatch.db'
    setup = sqlite3.connect(path, isolation_level=None)
    ExecutionDispatch(setup, Adapter())
    setup.close()
    class ConcurrentAdapter(Adapter):
        def start(self, request):
            self.starts += 1
            return ExecutionObservation(request.execution_id, 'owner', self.instance, 0, 'accepted')
        def inspect(self, execution_id):
            return ExecutionObservation(execution_id, 'owner', self.instance, 0, 'accepted')
    adapter = ConcurrentAdapter()
    def dispatch_start():
        db = sqlite3.connect(path, isolation_level=None, timeout=2)
        try:
            return ExecutionDispatch(db, adapter).start(request())
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: dispatch_start(), range(2)))
    assert adapter.starts == 1, 'same durable intent was sent to the executor twice'
    assert len(results) == 2


def test_history_catchup_accepts_identical_events_committed_by_other_connection(tmp_path):
    path = tmp_path / 'history-race.db'
    db = sqlite3.connect(path, isolation_level=None)
    other_db = sqlite3.connect(path, isolation_level=None)
    adapter = Adapter()
    first = ExecutionDispatch(db, adapter)
    second = ExecutionDispatch(other_db, adapter)
    first.start(request())
    running = ExecutionObservation('e1', 'owner', 'i1', 2, 'running', metadata={'stdout': 'partial'})
    final = ExecutionObservation('e1', 'owner', 'i1', 3, 'success', exit_code=0, tree_exit_confirmed=True)
    original_events = adapter.events
    def concurrent_events(execution_id, after_sequence=-1):
        values = [running, final]
        second.record(running)
        second.record(final)
        return values
    adapter.events = concurrent_events
    adapter.inspect = lambda execution_id: final
    try:
        assert first.reconcile('e1') == final
        assert db.execute('SELECT COUNT(*) FROM execution_dispatch_events').fetchone()[0] == 4
    finally:
        db.close()
        other_db.close()


def test_history_catchup_rejects_conflict_committed_by_other_connection(tmp_path):
    from dataclasses import replace
    path = tmp_path / 'history-conflict.db'
    db = sqlite3.connect(path, isolation_level=None)
    other_db = sqlite3.connect(path, isolation_level=None)
    adapter = Adapter()
    first = ExecutionDispatch(db, adapter)
    second = ExecutionDispatch(other_db, adapter)
    first.start(request())
    running = ExecutionObservation('e1', 'owner', 'i1', 2, 'running', metadata={'stdout': 'actual'})
    final = ExecutionObservation('e1', 'owner', 'i1', 3, 'success', exit_code=0, tree_exit_confirmed=True)
    def concurrent_events(execution_id, after_sequence=-1):
        second.record(running)
        second.record(final)
        return [replace(running, metadata={'stdout': 'forged'}), final]
    adapter.events = concurrent_events
    adapter.inspect = lambda execution_id: final
    try:
        with pytest.raises(ValueError, match='execution_dispatch_event_conflict'):
            first.reconcile('e1')
        assert db.execute('SELECT last_sequence FROM execution_dispatch').fetchone()[0] == 3
        assert 'actual' in db.execute('SELECT observation FROM execution_dispatch_events WHERE sequence=2').fetchone()[0]
    finally:
        db.close()
        other_db.close()

def test_legacy_dispatch_reconcile_restores_complete_history():
    db = sqlite3.connect(':memory:', isolation_level=None)
    db.execute('CREATE TABLE execution_dispatch (execution_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, executor_id TEXT NOT NULL, instance_id TEXT NOT NULL, delivery_state TEXT NOT NULL, last_sequence INTEGER NOT NULL, last_state TEXT NOT NULL, updated_at REAL NOT NULL)')
    db.execute("INSERT INTO execution_dispatch VALUES ('e1', ?, 'owner', 'i1', 'unconfirmed', 1, 'running', 0)", ('a' * 64,))
    adapter = Adapter()
    dispatch = ExecutionDispatch(db, adapter)
    try:
        assert dispatch.reconcile('e1').state == 'running'
        assert [row[0] for row in db.execute('SELECT sequence FROM execution_dispatch_events ORDER BY sequence')] == [0, 1]
        assert adapter.starts == 0
    finally:
        db.close()

def test_reconcile_stale_snapshot_returns_current_verified_observation():
    db = sqlite3.connect(':memory:', isolation_level=None)
    adapter = Adapter()
    dispatch = ExecutionDispatch(db, adapter)
    try:
        dispatch.start(request())
        stale = adapter.inspect('e1')
        latest = ExecutionObservation('e1', 'owner', 'i1', 2, 'success', exit_code=0, tree_exit_confirmed=True)
        dispatch.record(latest)
        adapter.inspect = lambda _: stale
        assert dispatch.reconcile('e1') == latest
        assert adapter.starts == 1
        from dataclasses import replace
        adapter.inspect = lambda _: replace(stale, metadata={'stdout': 'conflicting'})
        with pytest.raises(ValueError, match='execution_dispatch_event_conflict'):
            dispatch.reconcile('e1')
    finally:
        db.close()

def test_legacy_history_rejects_state_regression_without_partial_writes():
    db = sqlite3.connect(':memory:', isolation_level=None)
    db.execute('CREATE TABLE execution_dispatch (execution_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, executor_id TEXT NOT NULL, instance_id TEXT NOT NULL, delivery_state TEXT NOT NULL, last_sequence INTEGER NOT NULL, last_state TEXT NOT NULL, updated_at REAL NOT NULL)')
    db.execute("INSERT INTO execution_dispatch VALUES ('e1', ?, 'owner', 'i1', 'unconfirmed', 2, 'accepted', 0)", ('a' * 64,))
    adapter = Adapter()
    adapter.inspect = lambda _: ExecutionObservation('e1', 'owner', 'i1', 2, 'accepted')
    adapter.events = lambda *args: [ExecutionObservation('e1', 'owner', 'i1', 0, 'accepted'), ExecutionObservation('e1', 'owner', 'i1', 1, 'running'), adapter.inspect('e1')]
    dispatch = ExecutionDispatch(db, adapter)
    try:
        with pytest.raises(ValueError, match='execution_dispatch_state_regression'):
            dispatch.reconcile('e1')
        assert db.execute('SELECT COUNT(*) FROM execution_dispatch_events').fetchone()[0] == 0
    finally:
        db.close()

def test_legacy_history_concurrent_reconcile_reuses_identical_batch(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    path = tmp_path / 'legacy-race.db'
    db = sqlite3.connect(path, isolation_level=None)
    db.execute('CREATE TABLE execution_dispatch (execution_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, executor_id TEXT NOT NULL, instance_id TEXT NOT NULL, delivery_state TEXT NOT NULL, last_sequence INTEGER NOT NULL, last_state TEXT NOT NULL, updated_at REAL NOT NULL)')
    db.execute("INSERT INTO execution_dispatch VALUES ('e1', ?, 'owner', 'i1', 'unconfirmed', 1, 'running', 0)", ('a' * 64,))
    ExecutionDispatch(db, Adapter())
    db.close()
    barrier = threading.Barrier(2)
    class History(Adapter):
        def inspect(self, execution_id):
            return ExecutionObservation(execution_id, 'owner', 'i1', 2, 'success', exit_code=0, tree_exit_confirmed=True)
        def events(self, execution_id, after_sequence=-1):
            barrier.wait(timeout=3)
            return [ExecutionObservation(execution_id, 'owner', 'i1', 0, 'accepted'),
                ExecutionObservation(execution_id, 'owner', 'i1', 1, 'running'), self.inspect(execution_id)]
    def reconcile():
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            return ExecutionDispatch(connection, History()).reconcile('e1')
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reconcile(), range(2)))
    assert all(result.state == 'success' for result in results)
    db = sqlite3.connect(path)
    try:
        assert db.execute('SELECT COUNT(*) FROM execution_dispatch_events').fetchone()[0] == 3
    finally:
        db.close()

def test_confirmed_unknown_exit_cannot_regress():
    db = sqlite3.connect(':memory:', isolation_level=None)
    dispatch = ExecutionDispatch(db, Adapter())
    dispatch.start(request())
    dispatch.record(ExecutionObservation('e1', 'owner', 'i1', 2, 'unknown', reason='stop_unconfirmed'))
    dispatch.record(ExecutionObservation('e1', 'owner', 'i1', 3, 'unknown', reason='stop_unconfirmed', tree_exit_confirmed=True))
    try:
        with pytest.raises(ValueError, match='execution_dispatch_terminal_conflict'):
            dispatch.record(ExecutionObservation('e1', 'owner', 'i1', 4, 'unknown', reason='stop_unconfirmed'))
        assert db.execute('SELECT last_sequence FROM execution_dispatch').fetchone()[0] == 3
    finally:
        db.close()

def test_legacy_history_restores_unknown_exit_confirmation():
    db = sqlite3.connect(':memory:', isolation_level=None)
    adapter = Adapter()
    dispatch = ExecutionDispatch(db, adapter)
    dispatch.start(request())
    values = [ExecutionObservation('e1', 'owner', 'i1', 0, 'accepted'),
        ExecutionObservation('e1', 'owner', 'i1', 1, 'running'),
        ExecutionObservation('e1', 'owner', 'i1', 2, 'unknown', reason='stop_unconfirmed'),
        ExecutionObservation('e1', 'owner', 'i1', 3, 'unknown', reason='stop_unconfirmed', tree_exit_confirmed=True)]
    db.execute('DELETE FROM execution_dispatch_events')
    adapter.inspect = lambda _: values[-1]
    adapter.events = lambda *args: values
    try:
        result = dispatch.reconcile('e1')
        assert result.state == 'unknown'
        assert result.tree_exit_confirmed
        assert db.execute('SELECT count(*) FROM execution_dispatch_events').fetchone()[0] == 4
        assert adapter.starts == 1
    finally:
        db.close()


def test_restart_reconciliation_only_accepts_original_identity_unknown_history():
    from dataclasses import replace
    class RestartAdapter(Adapter):
        def inspect(self, execution_id):
            if self.instance == 'i1':
                return super().inspect(execution_id)
            return ExecutionObservation(execution_id, 'owner', 'i1', 2, 'unknown',
                reason='executor_instance_lost', metadata={'recovery_instance_id': self.instance, 'previous_state': 'running'})
        def events(self, execution_id, after_sequence=-1):
            values = [ExecutionObservation(execution_id, 'owner', 'i1', 0, 'accepted'),
                      ExecutionObservation(execution_id, 'owner', 'i1', 1, 'running')]
            if self.instance != 'i1':
                values.append(self.inspect(execution_id))
            return [value for value in values if value.sequence > after_sequence]
    db = sqlite3.connect(':memory:', isolation_level=None)
    adapter = RestartAdapter()
    dispatch = ExecutionDispatch(db, adapter)
    dispatch.start(request())
    adapter.instance = 'i2'
    value = dispatch.reconcile('e1')
    assert value.state == 'unknown'
    assert value.instance_id == 'i1'
    assert not value.tree_exit_confirmed
    assert adapter.starts == 1
    assert db.execute('SELECT COUNT(*) FROM execution_dispatch_events').fetchone()[0] == 3
    assert dispatch.reconcile('e1') == value
