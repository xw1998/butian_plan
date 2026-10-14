"""Persist authorization for provider planning without creating business tasks."""
from __future__ import annotations

from contextlib import contextmanager

import hashlib
import json
import os
import errno
import time
import uuid


def initialize(store):
    store.db.execute('''CREATE TABLE IF NOT EXISTS planning_executions (
        planning_id TEXT PRIMARY KEY, member_id TEXT NOT NULL,
        provider_session_id TEXT UNIQUE, input_sha256 TEXT,
        authorization_snapshot TEXT NOT NULL, problem_revision INTEGER NOT NULL,
        status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)''')


@contextmanager
def _transaction(store):
    with store._write_lock:
        name = 'planning_' + uuid.uuid4().hex
        owns_transaction = not store.db.in_transaction
        if owns_transaction:
            store.db.execute('BEGIN IMMEDIATE')
        store.db.execute('SAVEPOINT ' + name)
        released = False
        try:
            yield
            store.db.execute('RELEASE ' + name)
            released = True
            if owns_transaction:
                store.db.execute('COMMIT')
        except BaseException:
            if not released:
                store.db.execute('ROLLBACK TO ' + name)
                store.db.execute('RELEASE ' + name)
            if owns_transaction and store.db.in_transaction:
                store.db.execute('ROLLBACK')
            raise

def _owner_lock(store, planning_id):
    directory = store.path.parent / '.planning-locks'
    from .execution_pairing import validate_local_path
    validate_local_path(directory, 'planning_owner_path_invalid')
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / (hashlib.sha256(planning_id.encode()).hexdigest() + '.lock')
    validate_local_path(lock_path, 'planning_owner_path_invalid')
    handle = lock_path.open('a+b')
    try:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return None
        raise

@contextmanager
def ownership(store):
    planning_id = 'planning_' + uuid.uuid4().hex
    handle = _owner_lock(store, planning_id)
    if handle is None:
        raise ValueError('planning_owner_conflict')
    try:
        yield planning_id
    finally:
        handle.close()

def create(store, member_id, problem_revision, *, planning_id=None):
    with _transaction(store):
        member = store.db.execute('SELECT tool_policy FROM members WHERE member_id=?', (member_id,)).fetchone()
        if member is None:
            raise ValueError('member_not_found')
        planning_id = planning_id or 'planning_' + uuid.uuid4().hex
        now = time.time()
        snapshot = json.dumps({'member_id': member_id, 'tool_policy': json.loads(member[0] or '{}')})
        store.db.execute('INSERT INTO planning_executions VALUES (?,?,NULL,NULL,?,?,?,?,?)',
            (planning_id, member_id, snapshot, problem_revision, 'prepared', now, now))
        store.append_event(planning_id, 'planning_prepared', {'member_id': member_id}, planning_id + ':prepared')
        return planning_id


def bind(store, planning_id, provider_session_id):
    with _transaction(store):
        session = store.provider_session(provider_session_id)
        record = store.db.execute('SELECT authorization_snapshot FROM planning_executions WHERE planning_id=?', (planning_id,)).fetchone()
        if record is None:
            raise ValueError('planning_execution_binding_conflict')
        snapshot = json.loads(record['authorization_snapshot'])
        snapshot['provider_id'] = session['provider_id']
        snapshot['remote_session_id'] = session['remote_session_id']
        changed = store.db.execute("UPDATE planning_executions SET authorization_snapshot=?,provider_session_id=?,input_sha256=?,status='running',updated_at=? WHERE planning_id=? AND member_id=? AND status='prepared'",
            (json.dumps(snapshot), provider_session_id, hashlib.sha256((session['prompt'] or '').encode('utf-8')).hexdigest(), time.time(), planning_id, session['member_id'])).rowcount
        if changed != 1:
            raise ValueError('planning_execution_binding_conflict')
        store.append_event(planning_id, 'planning_bound', {'provider_session_id': provider_session_id}, planning_id + ':bound')


def finish(store, planning_id, status):
    if status not in {'completed', 'failed', 'unknown', 'revoked'}:
        raise ValueError('planning_execution_status_invalid')
    with _transaction(store):
        changed = store.db.execute("UPDATE planning_executions SET status=?,updated_at=? WHERE planning_id=? AND status IN ('prepared','running')",
            (status, time.time(), planning_id)).rowcount
        if changed:
            store.append_event(planning_id, 'planning_' + status, {}, planning_id + ':' + status)


def active(store, planning_id, member_id, provider_session_id, input_sha256):
    row = store.db.execute('SELECT * FROM planning_executions WHERE planning_id=?', (planning_id,)).fetchone()
    member = store.db.execute('SELECT tool_policy FROM members WHERE member_id=?', (member_id,)).fetchone()
    session = store.db.execute('SELECT status,member_id,prompt,provider_id,remote_session_id FROM provider_sessions WHERE provider_session_id=?', (provider_session_id,)).fetchone()
    if not row or not member or not session or row['status'] != 'running' or row['member_id'] != member_id or row['provider_session_id'] != provider_session_id or row['input_sha256'] != input_sha256 or session['status'] != 'running' or session['member_id'] != member_id:
        return False
    if hashlib.sha256((session['prompt'] or '').encode('utf-8')).hexdigest() != row['input_sha256']:
        return False
    snapshot = json.loads(row['authorization_snapshot'])
    if (snapshot.get('member_id') != member_id
            or snapshot.get('provider_id') != session['provider_id']
            or snapshot.get('remote_session_id') != session['remote_session_id']):
        return False
    for policy in (snapshot.get('tool_policy', {}), json.loads(member[0] or '{}')):
        if (policy.get('default') in {'deny', 'deny_until_authorized'} or 'permissions' in policy) and 'execute' not in set(policy.get('permissions', [])):
            return False
    return True

def recover(store):
    """Revoke stale planning permission; never replay or claim process exit."""
    with store._write_lock:
        handles = []
        store.db.execute('SAVEPOINT planning_recovery')
        try:
            rows = store.db.execute("SELECT planning_id,provider_session_id FROM planning_executions WHERE status IN ('prepared','running') ORDER BY planning_id").fetchall()
            recovered = []
            handles = []
            for row in rows:
                handle = _owner_lock(store, row['planning_id'])
                if handle is None:
                    continue
                handles.append(handle)
                recovered.append(row['planning_id'])
                store.db.execute("UPDATE planning_executions SET status='unknown',updated_at=? WHERE planning_id=?", (time.time(), row['planning_id']))
                store.append_event(row['planning_id'], 'planning_owner_lost',
                    {'provider_session_id': row['provider_session_id'], 'tree_exit_confirmed': False}, row['planning_id'] + ':owner-lost')
            store.db.execute('RELEASE planning_recovery')
            return recovered
        except BaseException:
            store.db.execute('ROLLBACK TO planning_recovery')
            store.db.execute('RELEASE planning_recovery')
            raise
        finally:
            for handle in handles:
                handle.close()
