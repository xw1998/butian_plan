"""Business-side dispatch outbox; transport ambiguity never retries a start."""
from __future__ import annotations

from dataclasses import asdict

import json
import sqlite3
import threading
import time

from .execution_contract import ExecutionAdapter, ExecutionObservation, ExecutionRequest
from .execution_protocol import canonical


class ExecutionNotStarted(ValueError):
    """Durable cancellation exists and no dispatch intent was ever created."""

class ExecutionDispatch:
    def __init__(self, db: sqlite3.Connection, adapter: ExecutionAdapter, *, write_lock=None):
        self.db, self.adapter = db, adapter
        self._lock = write_lock or threading.RLock()
        db.execute('''CREATE TABLE IF NOT EXISTS execution_dispatch (
            execution_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
            executor_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            delivery_state TEXT NOT NULL, last_sequence INTEGER NOT NULL,
            last_state TEXT NOT NULL, updated_at REAL NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS execution_cancel_intents (
            execution_id TEXT PRIMARY KEY, reason TEXT NOT NULL, created_at REAL NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS execution_dispatch_events (
            execution_id TEXT NOT NULL, sequence INTEGER NOT NULL,
            observation TEXT NOT NULL, PRIMARY KEY(execution_id,sequence))''')
        with self._lock:
            columns = {row[1] for row in db.execute('PRAGMA table_info(execution_dispatch)')}
            if 'last_observation' not in columns:
                db.execute('ALTER TABLE execution_dispatch ADD COLUMN last_observation TEXT')

    def assert_not_cancelled(self, execution_id: str) -> None:
        with self._lock:
            if self.db.execute('SELECT 1 FROM execution_cancel_intents WHERE execution_id=?', (execution_id,)).fetchone():
                existing = self.db.execute('SELECT 1 FROM execution_dispatch WHERE execution_id=?', (execution_id,)).fetchone()
                error = ValueError if existing else ExecutionNotStarted
                raise error('execution_cancelled_before_dispatch')

    def cancel(self, execution_id: str, *, reason: str) -> ExecutionObservation:
        if not execution_id or not reason:
            raise ValueError('execution_cancel_invalid')
        with self._lock:
            if self.db.in_transaction:
                raise ValueError('execution_dispatch_requires_committed_transaction')
            self.db.execute('INSERT OR IGNORE INTO execution_cancel_intents VALUES (?,?,?)',
                            (execution_id, reason, time.time()))
            row = self.db.execute('SELECT 1 FROM execution_dispatch WHERE execution_id=?', (execution_id,)).fetchone()
            if row is None:
                return ExecutionObservation(execution_id, '', '', 0, 'cancelling', reason=reason)
            return self._record_history(self.adapter.cancel(execution_id, reason=reason))

    def start(self, request: ExecutionRequest) -> ExecutionObservation:
        capabilities = self.adapter.capabilities()
        capabilities.require()
        with self._lock:
            self.assert_not_cancelled(request.execution_id)
            # Do not dispatch before a caller-owned transaction is committed.
            if self.db.in_transaction:
                raise ValueError('execution_dispatch_requires_committed_transaction')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.assert_not_cancelled(request.execution_id)
                row = self.db.execute('SELECT request_sha256,executor_id,instance_id FROM execution_dispatch WHERE execution_id=?', (request.execution_id,)).fetchone()
                if row:
                    if row[0] != request.request_sha256:
                        raise ValueError('execution_id_request_conflict')
                    if (row[1], row[2]) != (capabilities.executor_id, capabilities.instance_id):
                        raise ValueError('execution_dispatch_owner_changed')
                    created = False
                else:
                    self.db.execute('INSERT INTO execution_dispatch (execution_id,request_sha256,executor_id,instance_id,delivery_state,last_sequence,last_state,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                                    (request.execution_id, request.request_sha256, capabilities.executor_id,
                                     capabilities.instance_id, 'prepared', -1, 'accepted', time.time()))
                    created = True
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        # A prepared record may mean a previous process died after sending. Query
        # the original owner rather than automatically replaying a side effect.
        try:
            with self._lock:
                self.assert_not_cancelled(request.execution_id)
                value = self.adapter.start(request) if created else self.adapter.inspect(request.execution_id)
                return self._record_history(value)
        except BaseException:
            with self._lock:
                self.db.execute("UPDATE execution_dispatch SET delivery_state='unconfirmed',updated_at=? WHERE execution_id=?", (time.time(), request.execution_id))
            raise

    def record(self, observation: ExecutionObservation) -> ExecutionObservation:
        serialized = canonical(asdict(observation)).decode('utf-8')
        with self._lock:
            self.db.execute('SAVEPOINT dispatch_observation')
            try:
                self.db.execute('UPDATE execution_dispatch SET updated_at=updated_at WHERE execution_id=?', (observation.execution_id,))
                row = self.db.execute('SELECT executor_id,instance_id,last_sequence,last_state,last_observation FROM execution_dispatch WHERE execution_id=?', (observation.execution_id,)).fetchone()
                if not row or (row[0], row[1]) != (observation.executor_id, observation.instance_id):
                    raise ValueError('execution_dispatch_owner_changed')
                if observation.sequence > row[2] + 1:
                    raise ValueError('execution_dispatch_event_sequence_gap')
                if observation.sequence < row[2] or (observation.sequence == row[2] and observation.state != row[3]):
                    raise ValueError('execution_dispatch_event_conflict')
                if observation.sequence == row[2] and row[4] is not None and row[4] != serialized:
                    raise ValueError('execution_dispatch_event_conflict')
                if observation.sequence > row[2] and (
                        (row[3] == 'running' and observation.state == 'accepted')
                        or (row[3] == 'cancelling' and observation.state in {'accepted', 'running'})):
                    raise ValueError('execution_dispatch_state_regression')
                if row[3] in {'success', 'failed', 'cancelled', 'unknown'} and observation.state != row[3]:
                    raise ValueError('execution_dispatch_terminal_conflict')
                if observation.sequence > row[2] and row[3] in {'success', 'failed', 'cancelled', 'unknown'} and row[4] is not None:
                    previous = ExecutionObservation(**json.loads(row[4]))
                    if not (previous.state == observation.state == 'unknown'
                            and not previous.tree_exit_confirmed and observation.tree_exit_confirmed
                            and previous.reason == observation.reason):
                        raise ValueError('execution_dispatch_terminal_conflict')
                self.db.execute("UPDATE execution_dispatch SET delivery_state='acknowledged',last_sequence=?,last_state=?,updated_at=?,last_observation=? WHERE execution_id=?",
                                (observation.sequence, observation.state, time.time(), serialized, observation.execution_id))
                self.db.execute('INSERT OR IGNORE INTO execution_dispatch_events VALUES (?,?,?)',
                                (observation.execution_id, observation.sequence, serialized))
                self.db.execute('RELEASE dispatch_observation')
                return observation
            except BaseException:
                self.db.execute('ROLLBACK TO dispatch_observation')
                self.db.execute('RELEASE dispatch_observation')
                raise

    def reconcile(self, execution_id: str) -> ExecutionObservation:
        with self._lock:
            row = self.db.execute('SELECT executor_id,instance_id FROM execution_dispatch WHERE execution_id=?', (execution_id,)).fetchone()
        if row is None:
            raise ValueError('execution_dispatch_not_found')
        capabilities = self.adapter.capabilities()
        if tuple(row) != (capabilities.executor_id, capabilities.instance_id):
            if row[0] != capabilities.executor_id:
                raise ValueError('execution_dispatch_owner_changed')
            recovered = self.adapter.inspect(execution_id)
            if ((recovered.executor_id, recovered.instance_id) != tuple(row)
                    or recovered.execution_id != execution_id
                    or recovered.state != 'unknown'
                    or recovered.reason != 'executor_instance_lost'
                    or recovered.tree_exit_confirmed
                    or recovered.metadata.get('recovery_instance_id') != capabilities.instance_id):
                raise ValueError('execution_dispatch_owner_changed')
            return self._record_history(recovered)
        return self._record_history(self.adapter.inspect(execution_id))

    def _record_history(self, observation: ExecutionObservation) -> ExecutionObservation:
        execution_id = observation.execution_id
        with self._lock:
            row = self.db.execute('SELECT last_sequence,executor_id,instance_id,last_state,last_observation FROM execution_dispatch WHERE execution_id=?', (execution_id,)).fetchone()
            last = row[0]
            count = self.db.execute('SELECT COUNT(*) FROM execution_dispatch_events WHERE execution_id=? AND sequence BETWEEN 0 AND ?', (execution_id, last)).fetchone()[0]
        if last >= 0 and count != last + 1:
            values = self.adapter.events(execution_id, -1)
            if not values or any(value.execution_id != execution_id or value.sequence != index
                    or (value.executor_id, value.instance_id) != (row[1], row[2])
                    for index, value in enumerate(values)) or values[-1].sequence < max(last, observation.sequence):
                raise ValueError('execution_dispatch_event_sequence_gap')
            if values[0].state != 'accepted':
                raise ValueError('execution_dispatch_state_regression')
            transitions = {'accepted': {'running', 'cancelling', 'failed', 'unknown'},
                'running': {'cancelling', 'success', 'failed', 'unknown'},
                'cancelling': {'cancelled', 'failed', 'unknown'}}
            if any(right.state not in transitions.get(left.state, set())
                    and not (left.state == right.state == 'unknown'
                        and not left.tree_exit_confirmed and right.tree_exit_confirmed
                        and left.reason == right.reason)
                    for left, right in zip(values, values[1:])):
                raise ValueError('execution_dispatch_state_regression')
            snapshot = values[observation.sequence]
            if canonical(asdict(snapshot)) != canonical(asdict(observation)):
                raise ValueError('execution_dispatch_event_conflict')
            previous = values[last]
            if previous.state != row[3] or (row[4] is not None and row[4] != canonical(asdict(previous)).decode('utf-8')):
                raise ValueError('execution_dispatch_event_conflict')
            with self._lock:
                self.db.execute('SAVEPOINT dispatch_legacy_history')
                try:
                    self.db.execute('UPDATE execution_dispatch SET updated_at=updated_at WHERE execution_id=?', (execution_id,))
                    for value in values[:last + 1]:
                        serialized = canonical(asdict(value)).decode('utf-8')
                        saved = self.db.execute('SELECT observation FROM execution_dispatch_events WHERE execution_id=? AND sequence=?', (execution_id, value.sequence)).fetchone()
                        if saved is not None and saved[0] != serialized:
                            raise ValueError('execution_dispatch_event_conflict')
                        self.db.execute('INSERT OR IGNORE INTO execution_dispatch_events VALUES (?,?,?)', (execution_id, value.sequence, serialized))
                    for value in values[last + 1:]:
                        self.record(value)
                    current = self.db.execute('SELECT last_observation FROM execution_dispatch WHERE execution_id=?', (execution_id,)).fetchone()[0]
                    if values[-1].sequence == last and current is None:
                        self.record(values[-1])
                    self.db.execute('RELEASE dispatch_legacy_history')
                    return values[-1]
                except BaseException:
                    self.db.execute('ROLLBACK TO dispatch_legacy_history')
                    self.db.execute('RELEASE dispatch_legacy_history')
                    raise
        if observation.sequence > last + 1:
            values = self.adapter.events(execution_id, last)
            expected = last + 1
            for value in values:
                if value.execution_id != execution_id or value.sequence != expected:
                    raise ValueError('execution_dispatch_event_sequence_gap')
                expected += 1
            if not values or values[-1].sequence < observation.sequence:
                raise ValueError('execution_dispatch_event_sequence_gap')
            snapshot = next((value for value in values if value.sequence == observation.sequence), None)
            if snapshot is None or canonical(asdict(snapshot)) != canonical(asdict(observation)):
                raise ValueError('execution_dispatch_event_conflict')
            with self._lock:
                self.db.execute('SAVEPOINT dispatch_history_batch')
                try:
                    # Reserve the SQLite writer before re-reading the cursor;
                    # another connection may have committed this same batch
                    # while events() was in flight.
                    self.db.execute('UPDATE execution_dispatch SET updated_at=updated_at WHERE execution_id=?', (execution_id,))
                    for value in values:
                        current = self.db.execute('SELECT last_sequence FROM execution_dispatch WHERE execution_id=?', (execution_id,)).fetchone()[0]
                        if value.sequence <= current:
                            saved = self.db.execute('SELECT observation FROM execution_dispatch_events WHERE execution_id=? AND sequence=?', (execution_id, value.sequence)).fetchone()
                            if saved is None or saved[0] != canonical(asdict(value)).decode('utf-8'):
                                raise ValueError('execution_dispatch_event_conflict')
                        else:
                            self.record(value)
                    if values[-1].sequence == observation.sequence:
                        result = self.record(observation)
                    else:
                        result = values[-1]
                    self.db.execute('RELEASE dispatch_history_batch')
                    return result
                except BaseException:
                    self.db.execute('ROLLBACK TO dispatch_history_batch')
                    self.db.execute('RELEASE dispatch_history_batch')
                    raise
        with self._lock:
            current = self.db.execute('SELECT last_sequence,last_observation FROM execution_dispatch WHERE execution_id=?', (execution_id,)).fetchone()
            if observation.sequence < current[0]:
                saved = self.db.execute('SELECT observation FROM execution_dispatch_events WHERE execution_id=? AND sequence=?', (execution_id, observation.sequence)).fetchone()
                if saved is None or saved[0] != canonical(asdict(observation)).decode('utf-8') or current[1] is None:
                    raise ValueError('execution_dispatch_event_conflict')
                import json
                return ExecutionObservation(**json.loads(current[1]))
        return self.record(observation)
