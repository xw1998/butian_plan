"""Executor-owned intents and ordered observations, independent of business DB."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict

from .execution_contract import ExecutionObservation, TERMINAL_STATES
from .execution_protocol import canonical

_TRANSITIONS = {
    "accepted": {"running", "cancelling", "failed", "unknown"},
    "running": {"cancelling", "success", "failed", "unknown"},
    "cancelling": {"cancelled", "failed", "unknown"},
    "unknown": set(), "success": set(), "failed": set(), "cancelled": set(),
}


class ExecutionLedger:
    def __init__(self, db: sqlite3.Connection, executor_id: str, instance_id: str):
        self.db, self.executor_id, self.instance_id = db, executor_id, instance_id
        self._lock = threading.RLock()
        db.executescript("""
        CREATE TABLE IF NOT EXISTS executor_runs (
            execution_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
            executor_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            state TEXT NOT NULL, sequence INTEGER NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS executor_events (
            execution_id TEXT NOT NULL, sequence INTEGER NOT NULL, observation TEXT NOT NULL,
            PRIMARY KEY(execution_id, sequence));
        """)

    def accept(self, execution_id: str, request_sha256: str) -> tuple[ExecutionObservation, bool]:
        """Persist intent before spawn; a repeated ID never grants another spawn."""
        if not execution_id or len(request_sha256) != 64 or any(c not in '0123456789abcdef' for c in request_sha256):
            raise ValueError('execution_intent_invalid')
        with self._lock:
            self.db.execute('SAVEPOINT executor_accept')
            try:
                # Acquire SQLite write reservation before the eligibility read.
                self.db.execute('UPDATE executor_runs SET sequence=sequence WHERE execution_id=?', (execution_id,))
                existing = self.db.execute('SELECT request_sha256 FROM executor_runs WHERE execution_id=?', (execution_id,)).fetchone()
                if existing:
                    if existing[0] != request_sha256:
                        raise ValueError('execution_id_request_conflict')
                    result, created = self.inspect(execution_id), False
                else:
                    result = ExecutionObservation(execution_id, self.executor_id, self.instance_id, 0, 'accepted')
                    self.db.execute('INSERT INTO executor_runs VALUES (?,?,?,?,?,?,?)',
                                    (execution_id, request_sha256, self.executor_id, self.instance_id, 'accepted', 0, time.time()))
                    self.db.execute('INSERT INTO executor_events VALUES (?,?,?)', (execution_id, 0, canonical(asdict(result)).decode()))
                    created = True
                self.db.execute('RELEASE executor_accept')
                return result, created
            except BaseException:
                self.db.execute('ROLLBACK TO executor_accept')
                self.db.execute('RELEASE executor_accept')
                raise

    def inspect(self, execution_id: str) -> ExecutionObservation:
        with self._lock:
            row = self.db.execute('SELECT e.observation FROM executor_events e JOIN executor_runs r ON r.execution_id=e.execution_id AND r.sequence=e.sequence WHERE e.execution_id=?', (execution_id,)).fetchone()
            if row is None:
                raise ValueError('execution_not_found')
            return ExecutionObservation(**json.loads(row[0]))

    def append(self, observation: ExecutionObservation) -> None:
        with self._lock:
            self.db.execute('SAVEPOINT executor_event')
            try:
                self.db.execute('UPDATE executor_runs SET sequence=sequence WHERE execution_id=?', (observation.execution_id,))
                previous = self.inspect(observation.execution_id)
                if (observation.executor_id != previous.executor_id or observation.instance_id != previous.instance_id
                        or observation.instance_id != self.instance_id):
                    raise ValueError('execution_event_identity_mismatch')
                if observation.sequence != previous.sequence + 1:
                    raise ValueError('execution_event_sequence_gap')
                exit_confirmation = (previous.state == observation.state == 'unknown'
                    and not previous.tree_exit_confirmed and observation.tree_exit_confirmed
                    and observation.reason == previous.reason)
                if observation.state not in _TRANSITIONS[previous.state] and not exit_confirmation:
                    raise ValueError('execution_state_transition_invalid')
                self.db.execute('INSERT INTO executor_events VALUES (?,?,?)',
                                (observation.execution_id, observation.sequence, canonical(asdict(observation)).decode()))
                self.db.execute('UPDATE executor_runs SET state=?, sequence=? WHERE execution_id=?',
                                (observation.state, observation.sequence, observation.execution_id))
                self.db.execute('RELEASE executor_event')
            except BaseException:
                self.db.execute('ROLLBACK TO executor_event')
                self.db.execute('RELEASE executor_event')
                raise

    def events(self, execution_id: str, after_sequence: int = -1) -> list[ExecutionObservation]:
        self.inspect(execution_id)
        with self._lock:
            return [ExecutionObservation(**json.loads(row[0])) for row in self.db.execute(
                'SELECT observation FROM executor_events WHERE execution_id=? AND sequence>? ORDER BY sequence',
                (execution_id, after_sequence))]

    def previous_unconfirmed(self) -> list[ExecutionObservation]:
        """Previous generations remain visible; never adopt their PID or replay."""
        with self._lock:
            ids = [row[0] for row in self.db.execute(
                "SELECT execution_id FROM executor_runs WHERE instance_id!=? AND state IN ('accepted','running','cancelling','unknown')",
                (self.instance_id,))]
            return [self.inspect(execution_id) for execution_id in ids]

    def recover_previous_instances(self) -> list[ExecutionObservation]:
        """Record lost ownership without adopting processes or claiming exit."""
        with self._lock:
            self.db.execute('SAVEPOINT executor_recovery')
            try:
                self.db.execute('UPDATE executor_runs SET sequence=sequence WHERE executor_id=?',
                                (self.executor_id,))
                ids = [row[0] for row in self.db.execute(
                    "SELECT execution_id FROM executor_runs WHERE executor_id=? AND instance_id!=? AND state IN ('accepted','running','cancelling') ORDER BY execution_id",
                    (self.executor_id, self.instance_id))]
                recovered = []
                for execution_id in ids:
                    previous = self.inspect(execution_id)
                    value = ExecutionObservation(execution_id, previous.executor_id,
                        previous.instance_id, previous.sequence + 1, 'unknown',
                        reason='executor_instance_lost',
                        metadata={'recovery_instance_id': self.instance_id,
                                  'previous_state': previous.state})
                    self.db.execute('INSERT INTO executor_events VALUES (?,?,?)',
                        (execution_id, value.sequence, canonical(asdict(value)).decode()))
                    self.db.execute('UPDATE executor_runs SET state=?,sequence=? WHERE execution_id=?',
                        ('unknown', value.sequence, execution_id))
                    recovered.append(value)
                self.db.execute('RELEASE executor_recovery')
                return recovered
            except BaseException:
                self.db.execute('ROLLBACK TO executor_recovery')
                self.db.execute('RELEASE executor_recovery')
                raise
