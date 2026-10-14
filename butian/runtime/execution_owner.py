"""Explicit executor owner: durable intent, bounded output and lease-controlled jobs."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import replace
from typing import Callable

from .execution_contract import CapabilityEvidence, ExecutionCapabilities, ExecutionObservation, ExecutionRequest, REQUIRED_CONTROL
from .execution_job import OwnedProcess
from .execution_ledger import ExecutionLedger
from .execution_permit import validate_permit


class LocalExecutionOwner:
    def __init__(self, db: sqlite3.Connection, executor_id: str, *,
                 authorization_active: Callable[[str, int], bool], process_factory=OwnedProcess):
        self.executor_id, self.instance_id = executor_id, uuid.uuid4().hex
        self.ledger = ExecutionLedger(db, executor_id, self.instance_id)
        self.ledger.recover_previous_instances()
        self.authorization_active, self.process_factory = authorization_active, process_factory
        self._features = {}
        # Owner state and ledger share one connection and one reentrant lock.
        self._lock = self.ledger._lock
        self._active = {}
        db.execute('CREATE TABLE IF NOT EXISTS executor_revocations (authorization_id TEXT NOT NULL,generation INTEGER NOT NULL,PRIMARY KEY(authorization_id,generation))')
        self._revoked = {tuple(row) for row in db.execute('SELECT authorization_id,generation FROM executor_revocations')}
        db.execute('CREATE TABLE IF NOT EXISTS executor_permit_leases (authorization_id TEXT NOT NULL,generation INTEGER NOT NULL,expires_at REAL NOT NULL,PRIMARY KEY(authorization_id,generation))')
        self._closing = False

    def capabilities(self):
        with self._lock:
            return ExecutionCapabilities(self.executor_id, self.instance_id, dict(self._features))

    def record_self_test(self, confirmed: bool, *, source: str):
        # Called by explicit bounded self-test, never from ordinary capability discovery.
        with self._lock:
            for name in REQUIRED_CONTROL:
                self._features[name] = CapabilityEvidence('supported' if confirmed else 'unsupported',
                                                         source, time.time(), self.instance_id)
            self._features['stream_output'] = CapabilityEvidence('supported' if confirmed else 'unknown', source, time.time(), self.instance_id)
            self._features['reconnect'] = CapabilityEvidence('supported', 'durable_ledger', time.time(), self.instance_id)
            for name in ('filesystem_isolation', 'network_isolation', 'memory_limit', 'file_size_limit'):
                self._features[name] = CapabilityEvidence('unsupported', 'same_account_execution', time.time(), self.instance_id)

    def _authorized(self, identity, generation):
        if (identity, generation) in self._revoked:
            return False
        row = self.ledger.db.execute('SELECT expires_at FROM executor_permit_leases WHERE authorization_id=? AND generation=?', (identity, generation)).fetchone()
        return bool(row and row[0] > time.time() and self.authorization_active(identity, generation))

    def authorize(self, authorization_id, generation, expires_at):
        import math
        if not isinstance(authorization_id, str) or not authorization_id or isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError('executor_authorization_invalid')
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at) or not 0 < expires_at - time.time() <= 60:
            raise ValueError('executor_authorization_expired')
        with self._lock:
            if (authorization_id, generation) in self._revoked:
                raise ValueError('execution_permit_authorization_revoked')
            self.ledger.db.execute('INSERT INTO executor_permit_leases VALUES (?,?,?) ON CONFLICT(authorization_id,generation) DO UPDATE SET expires_at=excluded.expires_at', (authorization_id, generation, expires_at))
            return {'authorization_id': authorization_id, 'generation': generation, 'expires_at': expires_at}

    def start(self, request: ExecutionRequest):
        with self._lock:
            if self._closing:
                raise ValueError('executor_closing')
            validate_permit(request, self.capabilities(), self._authorized)
            accepted, created = self.ledger.accept(request.execution_id, request.request_sha256)
            if not created:
                return accepted
            process = None
            worker_started = False
            try:
                process = self.process_factory(request.argv, request.cwd, request.stdin)
                active = {'process': process, 'request': request, 'lease': time.monotonic() + 10,
                          'deadline': time.monotonic() + request.timeout, 'reason': None,
                          'stdout': bytearray(), 'stderr': bytearray(), 'overflow': False,
                          'stop_error': None, 'finished': threading.Event()}
                self._active[request.execution_id] = active
                self._append(request.execution_id, 'running')
                worker = threading.Thread(target=self._run, args=(request.execution_id, active), daemon=True)
                active['worker'] = worker
                worker.start()
                worker_started = True
                return self.ledger.inspect(request.execution_id)
            except BaseException as exc:
                if process is None and getattr(exc, 'owned_process', None) is not None:
                    process = exc.owned_process
                    self._active[request.execution_id] = {
                        'process': process, 'request': request, 'worker': None,
                        'reason': 'launch_unconfirmed', 'stop_error': None,
                        'gate_cleanup_error': getattr(exc, 'gate_cleanup_error', None)}
                if process is not None and not worker_started and getattr(exc, 'owned_process', None) is None:
                    try:
                        process.abort_unreleased()
                        if process.confirmed_exit():
                            process.close()
                            self._active.pop(request.execution_id, None)
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as cleanup_exc:
                        # A gate timeout is not exit evidence and must not replace
                        # the launch error or suppress its durable unknown event.
                        active = self._active.get(request.execution_id)
                        if active is not None:
                            active['gate_cleanup_error'] = type(cleanup_exc).__name__
                try:
                    self._append(request.execution_id, 'unknown', reason='launch_unconfirmed',
                                 metadata={'error_type': type(exc).__name__, 'error_code': getattr(exc, 'winerror', getattr(exc, 'errno', None))})
                except (sqlite3.Error, OSError, RuntimeError, ValueError) as audit_exc:
                    active = self._active.get(request.execution_id)
                    previous = self.ledger.inspect(request.execution_id)
                    observation = ExecutionObservation(request.execution_id, self.executor_id, self.instance_id,
                        previous.sequence + 1, 'unknown', reason='audit_persistence_failure',
                        metadata={'audit_persisted': False, 'launch_error_type': type(exc).__name__,
                                  'audit_error_type': type(audit_exc).__name__})
                    if active is None:
                        active = {'process': process, 'request': request, 'reason': 'launch_unconfirmed',
                                  'stop_error': None, 'worker': None}
                        self._active[request.execution_id] = active
                    active['unpersisted_observation'] = observation
                    if process is not None:
                        active['unpersisted_live_owner'] = True
                raise

    def _append(self, execution_id, state, **kwargs):
        previous = self.ledger.inspect(execution_id)
        value = ExecutionObservation(execution_id, self.executor_id, self.instance_id,
                                     previous.sequence + 1, state, **kwargs)
        self.ledger.append(value)
        return value

    def _stop(self, execution_id, active, reason):
        if active['stop_error'] is not None or active['reason'] is not None:
            return
        active['reason'] = reason
        active['stop_deadline'] = time.monotonic() + 5
        try:
            self._append(execution_id, 'cancelling', reason=reason)
        except (sqlite3.Error, OSError, RuntimeError, ValueError) as exc:
            # Audit failure must not suppress the original owner's stop path.
            # Its type is retained; it never supplies successful exit evidence.
            active['audit_error'] = type(exc).__name__
        try:
            active['process'].stop()
        except OSError as exc:
            active['stop_error'] = {'error_type': type(exc).__name__, 'error_code': getattr(exc, 'winerror', getattr(exc, 'errno', None))}

    def _run(self, execution_id, active):
        owned = active['process']
        request = active['request']
        def read(name, pipe):
            try:
                while True:
                    value = pipe.read(8192)
                    if not value:
                        break
                    with self._lock:
                        remaining = request.output_limit - len(active[name])
                        active[name].extend(value[:max(0, remaining)])
                        if len(value) > remaining:
                            active['overflow'] = True
                            self._stop(execution_id, active, 'output_limit')
                            break
            except Exception as exc:
                with self._lock:
                    active['io_error'] = type(exc).__name__
                    self._stop(execution_id, active, 'io_failure')
            finally:
                pipe.close()
        readers = [threading.Thread(target=read, args=(name, getattr(owned.process, name)), daemon=True)
                   for name in ('stdout', 'stderr')]
        try:
            for reader in readers:
                reader.start()
            def release():
                try:
                    if hasattr(owned, 'prepare_gate'):
                        owned.prepare_gate()
                    with self._lock:
                        validate_permit(request, self.capabilities(), self._authorized)
                        if active['reason'] is not None:
                            return
                        if hasattr(owned, 'release_gate'):
                            owned.release_gate()
                        else:
                            owned.release()
                    if hasattr(owned, 'release_input'):
                        owned.release_input()
                except Exception as exc:
                    with self._lock:
                        active['io_error'] = type(exc).__name__
                        self._stop(execution_id, active, 'io_failure')
            writer = threading.Thread(target=release, daemon=True)
            writer.start()
            while True:
                with self._lock:
                    if owned.confirmed_exit():
                        break
                    now = time.monotonic()
                    if active['stop_error'] is not None or (active.get('stop_deadline') is not None and now >= active['stop_deadline']):
                        self._append(execution_id, 'unknown', reason='stop_unconfirmed', metadata=active['stop_error'] or {})
                        return
                    if now >= active['deadline']:
                        self._stop(execution_id, active, 'timeout')
                    elif now >= active['lease']:
                        self._stop(execution_id, active, 'control_lease_expired')
                active['finished'].wait(0.02)
            writer.join(timeout=2)
            for reader in readers:
                reader.join(timeout=2)
            with self._lock:
                if writer.is_alive() or any(reader.is_alive() for reader in readers):
                    self._append(execution_id, 'unknown', reason='execution_io_unconfirmed')
                    return
                code = owned.process.returncode
                reason = active['reason']
                state = ('failed' if reason in {'timeout', 'output_limit', 'io_failure'} else 'cancelled') if reason else ('success' if code == 0 else 'failed')
                self._append(execution_id, state, reason=reason or (None if code == 0 else 'nonzero_exit'),
                             exit_code=code, tree_exit_confirmed=True,
                             metadata={'stdout': active['stdout'].decode('utf-8', 'replace'),
                                       'stderr': active['stderr'].decode('utf-8', 'replace')})
        except BaseException as exc:
            with self._lock:
                previous = self.ledger.inspect(execution_id)
                if previous.state not in {'success', 'failed', 'cancelled', 'unknown'}:
                    try:
                        self._append(execution_id, 'unknown', reason='execution_owner_failure',
                                     metadata={'error_type': type(exc).__name__})
                    except (sqlite3.Error, OSError, RuntimeError, ValueError) as audit_exc:
                        active['audit_error'] = type(audit_exc).__name__
                        active['unpersisted_observation'] = ExecutionObservation(
                            execution_id, previous.executor_id, previous.instance_id,
                            previous.sequence + 1, 'unknown', reason='audit_persistence_failure',
                            metadata={'audit_persisted': False, 'error_type': type(audit_exc).__name__})
        finally:
            with self._lock:
                # Do not close a denied live job as a substitute termination path.
                try:
                    confirmed = owned.confirmed_exit()
                except (OSError, RuntimeError):
                    confirmed = False
                try:
                    if confirmed:
                        if active.get('unpersisted_observation') is None:
                            previous = self.ledger.inspect(execution_id)
                            if previous.state == 'unknown' and not previous.tree_exit_confirmed:
                                try:
                                    self.ledger.append(replace(previous, sequence=previous.sequence + 1,
                                        tree_exit_confirmed=True, metadata={**previous.metadata,
                                            'exit_confirmation_source': 'owned_job_query'}))
                                except (sqlite3.Error, OSError, RuntimeError, ValueError):
                                    return
                        owned.close()
                        active['resources_closed'] = True
                        if active.get('unpersisted_observation') is None:
                            self._active.pop(execution_id, None)
                finally:
                    active['finished'].set()

    def _release_confirmed_inactive(self, execution_id, active):
        if active.get('unpersisted_observation') is not None:
            return
        worker = active.get('worker')
        if worker is not None and worker.is_alive():
            return
        process = active.get('process')
        try:
            if process is not None and not active.get('resources_closed'):
                if not process.confirmed_exit():
                    return
                previous = self.ledger.inspect(execution_id)
                if previous.state == 'unknown' and not previous.tree_exit_confirmed:
                    self.ledger.append(replace(previous, sequence=previous.sequence + 1,
                        tree_exit_confirmed=True, metadata={**previous.metadata,
                            'exit_confirmation_source': 'owned_job_query'}))
                process.close()
                active['resources_closed'] = True
            self._active.pop(execution_id, None)
        except (sqlite3.Error, OSError, RuntimeError, ValueError):
            # Failed audit persistence, query or close cannot release ownership.
            return

    def inspect(self, execution_id):
        with self._lock:
            active = self._active.get(execution_id)
            if active and active.get('unpersisted_observation') is not None:
                pending = active['unpersisted_observation']
                recovered = replace(pending, metadata={**pending.metadata, 'audit_persisted': True})
                try:
                    self.ledger.append(recovered)
                except (sqlite3.Error, OSError, RuntimeError, ValueError):
                    return pending
                active.pop('unpersisted_observation', None)
                self._release_confirmed_inactive(execution_id, active)
                return self.ledger.inspect(execution_id)
            result = self.ledger.inspect(execution_id)
            if active and result.state in {'unknown', 'success', 'failed', 'cancelled'}:
                self._release_confirmed_inactive(execution_id, active)
                return self.ledger.inspect(execution_id)
            return result

    def events(self, execution_id, after_sequence=0):
        with self._lock:
            self.inspect(execution_id)
            return self.ledger.events(execution_id, after_sequence)

    def cancel(self, execution_id, *, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('execution_cancel_reason_required')
        with self._lock:
            active = self._active.get(execution_id)
            if active:
                self._stop(execution_id, active, reason)
            return self.inspect(execution_id)

    def renew(self, execution_id, lease_seconds):
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)) or not 0 < lease_seconds <= 10:
            raise ValueError('execution_lease_invalid')
        with self._lock:
            active = self._active.get(execution_id)
            if active is None or active['reason']:
                raise ValueError('execution_lease_not_running')
            permit = active['request'].permit
            if not self._authorized(permit['authorization_id'], permit['authorization_generation']):
                self._stop(execution_id, active, 'authorization_revoked')
                raise ValueError('execution_permit_authorization_revoked')
            active['lease'] = time.monotonic() + lease_seconds
            return {'execution_id': execution_id, 'lease_seconds': lease_seconds}

    def revoke(self, authorization_id, generation):
        with self._lock:
            self.ledger.db.execute('CREATE TABLE IF NOT EXISTS executor_revocations (authorization_id TEXT NOT NULL,generation INTEGER NOT NULL,PRIMARY KEY(authorization_id,generation))')
            self.ledger.db.execute('INSERT OR IGNORE INTO executor_revocations VALUES (?,?)', (authorization_id, generation))
            self._revoked.add((authorization_id, generation))
            ids = []
            for execution_id, active in self._active.items():
                permit = active['request'].permit
                if (permit['authorization_id'], permit['authorization_generation']) == (authorization_id, generation):
                    ids.append(execution_id)
                    self._stop(execution_id, active, 'authorization_revoked')
            return {'authorization_id': authorization_id, 'executions': ids}

    def close(self):
        with self._lock:
            self._closing = True
            for execution_id in list(self._active):
                self.inspect(execution_id)
                active = self._active.get(execution_id)
                if active is not None and active.get('process') is not None:
                    self._stop(execution_id, active, 'executor_shutdown')
            workers = [active['worker'] for active in self._active.values()
                       if active.get('worker') is not None and active['worker'].ident is not None]
        for worker in workers:
            worker.join(timeout=5)
        with self._lock:
            for execution_id in list(self._active):
                self.inspect(execution_id)
            if self._active:
                raise RuntimeError('shutdown_incomplete_active_tool_executions')
