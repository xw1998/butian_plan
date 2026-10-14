"""Business permits for explicitly configured local command providers."""
from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from dataclasses import replace

from .execution_contract import ExecutionRequest
from .execution_permit import capability_digest, request_digest


def provider_request_factory(store, home, runner):
    def permission_valid(task_id, member_id):
        row = store.db.execute('SELECT authorization_snapshot FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        member = store.db.execute('SELECT tool_policy FROM members WHERE member_id=?', (member_id,)).fetchone()
        if not row or not member:
            return False
        snapshot = json.loads(row['authorization_snapshot'] or '{}')
        if snapshot.get('member_id') != member_id:
            return False
        for policy in (snapshot.get('tool_policy', {}), json.loads(member['tool_policy'] or '{}')):
            allowed = set(policy.get('permissions', []))
            if (policy.get('default') in {'deny', 'deny_until_authorized'} or 'permissions' in policy) and 'execute' not in allowed:
                return False
        return True

    def factory(remote_session_id, prompt, command, timeout):
        with store._write_lock:
            if store.db.in_transaction:
                raise ValueError('execution_dispatch_requires_committed_transaction')
            session = store.db.execute('SELECT * FROM provider_sessions WHERE remote_session_id=? OR provider_session_id=?', (remote_session_id, remote_session_id)).fetchall()
            if len(session) != 1:
                raise ValueError('provider_execution_session_unverified')
            session = dict(session[0])
            task_id, member_id = session['task_id'], session['member_id']
            if session['provider_id'] != 'local-command' or session['status'] != 'running':
                raise ValueError('provider_execution_session_unverified')
            planning_id = None
            if task_id is None:
                from .planning_execution import active
                planning = store.db.execute('SELECT * FROM planning_executions WHERE provider_session_id=?', (session['provider_session_id'],)).fetchone()
                input_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
                if not planning or not active(store, planning['planning_id'], member_id, session['provider_session_id'], input_hash):
                    raise ValueError('provider_execution_planning_unverified')
                planning_id = task_id = planning['planning_id']
                revision = 0
                problem_revision = planning['problem_revision']
            else:
                task = store.db.execute('SELECT revision,payload,status FROM tasks WHERE task_id=?', (task_id,)).fetchone()
                if not task or not store.task_access(task_id, member_id) or task['status'] != 'running' or task['revision'] != session['task_revision']:
                    raise ValueError('provider_execution_task_unverified')
                if not permission_valid(task_id, member_id):
                    raise ValueError('provider_execution_authorization_denied')
                revision = task['revision']
                problem_revision = json.loads(task['payload']).get('problem_model', {}).get('revision', 0)
        capabilities = runner.adapter.capabilities()
        capabilities.require()
        execution_id = 'provider_exec_' + uuid.uuid4().hex
        root = home / 'provider_execution_work'
        from .execution_pairing import validate_local_path
        validate_local_path(root, 'provider_execution_path_invalid')
        root.mkdir(parents=True, exist_ok=True)
        work = root / execution_id
        work.mkdir()
        # This retained wrapper binds the configured command into a source hash.
        # It is not a privilege boundary; descendants remain owned by the executor.
        source = (work / 'command.py').resolve()
        content = ('import subprocess,sys\n'
                   'result = subprocess.run(' + repr(list(command)) + ', stdin=sys.stdin.buffer)\n'
                   'raise SystemExit(result.returncode)\n')
        with source.open('x', encoding='utf-8', newline='\n') as handle:
            handle.write(content)
        encoded = prompt.encode('utf-8')
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        from .execution_protocol import canonical
        manifest = {'command': list(command), 'permissions': ['execute']}
        manifest_hash = hashlib.sha256(canonical(manifest)).hexdigest()
        permit = {
            'member_id': member_id, 'task_id': task_id, 'problem_revision': problem_revision,
            'tool_id': 'provider:local-command', 'version': 0,
            'source_path': str(source), 'source_sha256': source_hash, 'manifest_sha256': manifest_hash, 'manifest': manifest,
            'input_sha256': hashlib.sha256(encoded).hexdigest(), 'permissions': ['execute'],
            'authorization_id': 'provider-business:' + execution_id, 'authorization_generation': 0,
            'expires_at': time.time() + 30, 'executor_id': capabilities.executor_id,
            'instance_id': capabilities.instance_id, 'capability_sha256': capability_digest(capabilities),
            'timeout': timeout, 'output_limit': 65536, 'work_budget_bytes': None,
        }
        request = ExecutionRequest(execution_id, '0' * 64, (sys.executable, '-I', str(source)), str(work.resolve()), encoded, timeout, 65536, permit)
        request = replace(request, request_sha256=request_digest(request))
        def authorization_valid():
            with store._write_lock:
                if planning_id is not None:
                    from .planning_execution import active
                    return active(store, planning_id, member_id, session['provider_session_id'], hashlib.sha256(encoded).hexdigest())
                current = store.db.execute('SELECT revision,payload,status FROM tasks WHERE task_id=?', (task_id,)).fetchone()
                current_session = store.db.execute('SELECT status,member_id FROM provider_sessions WHERE provider_session_id=?', (session['provider_session_id'],)).fetchone()
                return bool(current and current['revision'] == revision and current['status'] == 'running'
                            and json.loads(current['payload']).get('problem_model', {}).get('revision', 0) == problem_revision
                            and current_session and current_session['member_id'] == member_id and current_session['status'] == 'running'
                            and store.task_access(task_id, member_id)
                            and permission_valid(task_id, member_id))
        return request, authorization_valid
    return factory
