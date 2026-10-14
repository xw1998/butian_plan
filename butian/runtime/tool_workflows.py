"""Versioned sequential tool workflows with separate end-to-end verification."""
from __future__ import annotations
import json
import os
from pathlib import Path
import time
import uuid
from .tools import _contract_errors, _finite

class ToolWorkflows:
    def __init__(self, tools):
        self.tools = tools
        self.store = tools.store
        self.store.db.executescript('''
CREATE TABLE IF NOT EXISTS tool_workflows(workflow_id TEXT NOT NULL, version INTEGER NOT NULL, owner_member_id TEXT NOT NULL, name TEXT NOT NULL, manifest TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(workflow_id,version));
CREATE TABLE IF NOT EXISTS tool_workflow_runs(run_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, version INTEGER NOT NULL, owner_member_id TEXT NOT NULL, task_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tool_workflow_verifications(verification_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, evidence_ids TEXT NOT NULL, created_at REAL NOT NULL);
''')
        with self.store._write_lock, self._transaction():
            interrupted = self.store.db.execute("SELECT * FROM tool_workflow_runs WHERE status='running'").fetchall()
            for row in interrupted:
                handle = self._execution_lock(row['workflow_id'], row['version'])
                if handle is None:
                    continue
                try:
                    payload = json.loads(row['payload'])
                    payload['reason'] = 'workflow_execution_interrupted'
                    self.store.db.execute("UPDATE tool_workflow_runs SET status='unknown',payload=? WHERE run_id=?", (json.dumps(payload, ensure_ascii=False), row['run_id']))
                    self.store.append_event(row['workflow_id'], 'workflow_execution_recovered', {'run_id': row['run_id'], 'version': row['version'], 'status': 'unknown'}, 'workflow-recover:' + row['run_id'])
                finally:
                    handle.close()

    def get(self, owner, workflow_id, version):
        row = self.store.db.execute('SELECT * FROM tool_workflows WHERE workflow_id=? AND version=?', (workflow_id, version)).fetchone()
        if not row or row['owner_member_id'] != owner:
            raise ValueError('workflow_access_denied')
        return {**dict(row), 'manifest': json.loads(row['manifest'])}

    def create(self, owner, name, manifest, *, workflow_id=None):
        if not isinstance(name, str) or not name.strip() or not isinstance(manifest, dict):
            raise ValueError('workflow_manifest_invalid')
        for field in ('input_contract', 'output_contract', 'permissions', 'failure_conditions', 'steps', 'timeout_seconds', 'max_output_bytes'):
            if field not in manifest:
                raise ValueError('workflow_manifest_incomplete')
        if any(not isinstance(manifest[k], dict) or not manifest[k] for k in ('input_contract', 'output_contract')):
            raise ValueError('workflow_contract_invalid')
        if not isinstance(manifest['failure_conditions'], list) or not manifest['failure_conditions']:
            raise ValueError('workflow_failure_conditions_required')
        permissions = manifest['permissions']
        if not isinstance(permissions, list) or any(not isinstance(p, str) for p in permissions) or len(set(permissions)) != len(permissions):
            raise ValueError('workflow_permissions_invalid')
        timeout = _finite(manifest['timeout_seconds'], 'timeout_seconds', nonnegative=True)
        output = manifest['max_output_bytes']
        if not 0 < timeout <= 300 or type(output) is not int or not 0 < output <= 4 * 1024 * 1024:
            raise ValueError('workflow_budget_invalid')
        steps = manifest['steps']
        if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
            raise ValueError('workflow_steps_invalid')
        with self.store._write_lock, self._transaction():
            self.tools._member_dir(owner)
            locked = []
            for step in steps:
                if not isinstance(step, dict) or set(step) != {'tool_id', 'version'} or type(step['version']) is not int:
                    raise ValueError('workflow_step_invalid')
                tool = self.tools._tool(step['tool_id'])
                if tool['owner_member_id'] != owner or tool['status'] != 'active' or tool['current_version'] != step['version']:
                    raise ValueError('workflow_requires_stable_owned_tool')
                row = self.store.db.execute('SELECT content_hash,manifest FROM tool_versions WHERE tool_id=? AND version=?', (step['tool_id'], step['version'])).fetchone()
                contract = json.loads(row['manifest'])
                if not set(contract['permissions']).issubset(set(permissions)):
                    raise ValueError('workflow_permissions_incomplete')
                locked.append({**step, 'content_hash': row['content_hash'], 'manifest_hash': contract['manifest_hash']})
            if workflow_id:
                old = self.store.db.execute('SELECT owner_member_id,MAX(version) AS version FROM tool_workflows WHERE workflow_id=? GROUP BY owner_member_id', (workflow_id,)).fetchone()
                if not old or old['owner_member_id'] != owner:
                    raise ValueError('workflow_access_denied')
                version = old['version'] + 1
            else:
                workflow_id, version = 'workflow_' + uuid.uuid4().hex, 1
            saved = {**manifest, 'steps': locked}
            with self._transaction():
                self.store.db.execute('INSERT INTO tool_workflows VALUES (?,?,?,?,?,?)', (workflow_id, version, owner, name.strip(), json.dumps(saved, ensure_ascii=False, allow_nan=False), 'candidate'))
                self.store.append_event(workflow_id, 'workflow_created', {'version': version, 'owner_member_id': owner}, 'workflow-create:' + uuid.uuid4().hex)
            return self.get(owner, workflow_id, version)

    def retire(self, owner, workflow_id, version, *, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('workflow_reason_required')
        with self.store._write_lock, self._transaction():
            workflow = self.get(owner, workflow_id, version)
            if workflow['status'] == 'retired':
                return workflow
            if self.store.db.execute("SELECT 1 FROM tool_workflow_runs WHERE workflow_id=? AND version=? AND status IN ('running','unknown') LIMIT 1", (workflow_id, version)).fetchone():
                raise ValueError('workflow_has_inflight_executions')
            self.store.db.execute("UPDATE tool_workflows SET status='retired' WHERE workflow_id=? AND version=?", (workflow_id, version))
            self.store.append_event(workflow_id, 'workflow_retired', {'version': version, 'owner_member_id': owner, 'reason': reason.strip()}, 'workflow-retire:' + uuid.uuid4().hex)
            return self.get(owner, workflow_id, version)

    def _transaction(self):
        from contextlib import contextmanager
        @contextmanager
        def transaction():
            key = 'workflow_' + uuid.uuid4().hex
            self.store.db.execute('SAVEPOINT ' + key)
            try:
                self.store.db.execute('UPDATE tool_workflows SET status=status WHERE 0')
                yield
                self.store.db.execute('RELEASE ' + key)
            except BaseException:
                self.store.db.execute('ROLLBACK TO ' + key)
                self.store.db.execute('RELEASE ' + key)
                raise
        return transaction()

    def _verification_transaction(self):
        from contextlib import contextmanager
        @contextmanager
        def transaction():
            dependency_error = None
            with self._transaction():
                try:
                    yield
                except ValueError as exc:
                    if str(exc) != 'workflow_dependency_requires_reverification':
                        raise
                    dependency_error = exc
            if dependency_error is not None:
                raise dependency_error
        return transaction()

    def _check_dependencies(self, workflow):
        for step in workflow['manifest']['steps']:
            tool = self.tools._tool(step['tool_id'])
            row = self.store.db.execute('SELECT content_hash,manifest FROM tool_versions WHERE tool_id=? AND version=?', (step['tool_id'], step['version'])).fetchone()
            if tool['status'] != 'active' or tool['current_version'] != step['version'] or not row or row['content_hash'] != step['content_hash'] or json.loads(row['manifest']).get('manifest_hash') != step['manifest_hash']:
                with self.store._write_lock, self._transaction():
                    self.store.db.execute("UPDATE tool_workflows SET status='candidate' WHERE workflow_id=? AND version=?", (workflow['workflow_id'], workflow['version']))
                    self.store.append_event(workflow['workflow_id'], 'workflow_dependency_stale', {'version': workflow['version']}, 'workflow-stale:' + uuid.uuid4().hex)
                raise ValueError('workflow_dependency_requires_reverification')

    def _steps_verified(self, owner, workflow, row):
        payload = json.loads(row['payload'])
        results = payload.get('steps', [])
        steps = workflow['manifest']['steps']
        if row['owner_member_id'] != owner or len(results) != len(steps):
            return False
        for step, result in zip(steps, results):
            if result.get('tool_id') != step['tool_id'] or result.get('version') != step['version']:
                return False
            execution = self.store.db.execute('SELECT status,dry_run,actor_member_id,task_id,tool_id,version FROM tool_executions WHERE execution_id=?', (result.get('execution_id'),)).fetchone()
            if not execution or execution['status'] != 'success' or execution['dry_run'] or execution['actor_member_id'] != owner or execution['task_id'] != row['task_id'] or execution['tool_id'] != step['tool_id'] or execution['version'] != step['version']:
                return False
        return True

    def _independent_proofs(self, owner, workflow_id, version):
        proofs = self.store.db.execute('SELECT r.*,v.evidence_ids FROM tool_workflow_verifications v JOIN tool_workflow_runs r ON r.run_id=v.run_id WHERE r.workflow_id=? AND r.version=? AND r.status=? ORDER BY v.created_at,v.verification_id', (workflow_id, version, 'success')).fetchall()
        workflow = self.get(owner, workflow_id, version)
        tasks, seen, independent = set(), set(), 0
        for proof in proofs:
            if not self._steps_verified(owner, workflow, proof):
                continue
            hashes = set()
            for evidence in json.loads(proof['evidence_ids']):
                checked = self.store.verify_evidence(evidence, actor_member_id=owner)
                if checked['status'] != 'valid':
                    hashes = set()
                    break
                hashes.add(checked['sha256'])
            if not hashes or proof['task_id'] in tasks or hashes & seen:
                continue
            tasks.add(proof['task_id'])
            seen.update(hashes)
            independent += 1
        return independent

    def _execution_lock(self, workflow_id, version):
        import hashlib
        directory = Path(self.tools.home) / 'workflow_locks'
        directory.mkdir(exist_ok=True)
        key = hashlib.sha256((str(workflow_id) + ':' + str(version)).encode()).hexdigest()
        handle = (directory / (key + '.lock')).open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if handle.seek(0, 2) == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError as exc:
            handle.close()
            import errno
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return None
            raise

    def execute(self, owner, workflow_id, version, input_data, *, task_id, verification_only=False, dry_run=False):
        self.get(owner, workflow_id, version)
        handle = self._execution_lock(workflow_id, version)
        if handle is None:
            raise ValueError('workflow_execution_busy')
        try:
            return self._execute(owner, workflow_id, version, input_data, task_id=task_id, verification_only=verification_only, dry_run=dry_run)
        finally:
            handle.close()

    def _execute(self, owner, workflow_id, version, input_data, *, task_id, verification_only=False, dry_run=False):
        workflow = self.get(owner, workflow_id, version)
        if workflow['status'] == 'retired':
            raise ValueError('workflow_retired')
        if not verification_only and workflow['status'] != 'active':
            raise ValueError('workflow_requires_independent_verification')
        if not verification_only:
            with self.store._write_lock, self._transaction():
                stale = self._independent_proofs(owner, workflow_id, version) < 2
                if stale:
                    self.store.db.execute("UPDATE tool_workflows SET status='candidate' WHERE workflow_id=? AND version=?", (workflow_id, version))
                    self.store.append_event(workflow_id, 'workflow_verification_stale', {'version': version}, 'workflow-proof-stale:' + uuid.uuid4().hex)
            if stale:
                raise ValueError('workflow_verification_evidence_stale')
        manifest = workflow['manifest']
        if dry_run and len(manifest['steps']) > 1:
            raise ValueError('workflow_dry_run_intermediate_output_unavailable')
        self.tools.assert_task_authorization(owner, task_id, manifest['permissions'])
        if _contract_errors(input_data, manifest['input_contract']):
            raise ValueError('workflow_input_contract_invalid')
        self._check_dependencies(workflow)
        run_id = 'workflow_run_' + uuid.uuid4().hex
        started, output_bytes = time.monotonic(), 0
        value, results, status, reason = input_data, [], 'dry_run' if dry_run else 'success', None
        # Persist running intent before any side effect; an interrupted run is unknown.
        with self.store._write_lock, self._transaction():
            current = self.get(owner, workflow_id, version)
            if current['status'] == 'retired':
                raise ValueError('workflow_retired')
            if not verification_only and current['status'] != 'active':
                raise ValueError('workflow_requires_independent_verification')
            dependency_error = None
            try:
                self._check_dependencies(current)
            except ValueError as exc:
                if str(exc) != 'workflow_dependency_requires_reverification':
                    raise
                dependency_error = exc
            stale = not dependency_error and not verification_only and self._independent_proofs(owner, workflow_id, version) < 2
            if stale:
                self.store.db.execute("UPDATE tool_workflows SET status='candidate' WHERE workflow_id=? AND version=?", (workflow_id, version))
                self.store.append_event(workflow_id, 'workflow_verification_stale', {'version': version}, 'workflow-proof-stale:' + uuid.uuid4().hex)
            elif dependency_error is None:
                self.store.db.execute('INSERT INTO tool_workflow_runs VALUES (?,?,?,?,?,?,?)', (run_id, workflow_id, version, owner, task_id, 'running', json.dumps({'verification_only': verification_only})))
        if dependency_error is not None:
            raise dependency_error
        if stale:
            raise ValueError('workflow_verification_evidence_stale')
        try:
            for step in manifest['steps']:
                self._check_dependencies(workflow)
                remaining = manifest['timeout_seconds'] - (time.monotonic() - started)
                if remaining <= 0 or output_bytes >= manifest['max_output_bytes']:
                    status, reason = 'failed', 'workflow_budget_exhausted'
                    break
                result = self.tools.execute(owner, step['tool_id'], step['version'], value, task_id=task_id, dry_run=dry_run, timeout_seconds=remaining, max_output_bytes=manifest['max_output_bytes'] - output_bytes)
                results.append(result)
                if result.get('audit_status') == 'persistence_failed':
                    status, reason = 'unknown', 'workflow_step_audit_uncommitted'
                    break
                if result['status'] != ('dry_run' if dry_run else 'success'):
                    status, reason = result['status'], result.get('termination_reason')
                    break
                output_bytes += len(result.get('stdout', '').encode('utf-8')) + len(result.get('stderr', '').encode('utf-8'))
                if output_bytes > manifest['max_output_bytes'] or time.monotonic() - started > manifest['timeout_seconds']:
                    status, reason = 'failed', 'workflow_budget_exhausted'
                    break
                if not dry_run:
                    value = result['expected_output']
            if not dry_run and status == 'success' and _contract_errors(value, manifest['output_contract']):
                status, reason = 'failed', 'workflow_output_contract_invalid'
        except BaseException as exc:
            self._finish(run_id, workflow_id, version, 'unknown', {'verification_only': verification_only, 'steps': results, 'reason': type(exc).__name__})
            raise
        payload = {'verification_only': verification_only, 'steps': results, 'output': value if status == 'success' else None, 'reason': reason, 'output_bytes': output_bytes}
        self._finish(run_id, workflow_id, version, status, payload)
        return {'run_id': run_id, 'status': status, **payload}

    def _finish(self, run_id, workflow_id, version, status, payload):
        with self.store._write_lock, self._transaction():
            self.store.db.execute('UPDATE tool_workflow_runs SET status=?,payload=? WHERE run_id=?', (status, json.dumps(payload, ensure_ascii=False), run_id))
            self.store.append_event(workflow_id, 'workflow_execution', {'run_id': run_id, 'version': version, 'status': status}, 'workflow-run:' + run_id)

    def verify(self, owner, workflow_id, version, run_id, *, evidence_ids, expected_output):
        workflow = self.get(owner, workflow_id, version)
        if workflow['status'] == 'retired':
            raise ValueError('workflow_retired')
        self._check_dependencies(workflow)
        with self.store._write_lock, self._verification_transaction():
            workflow = self.get(owner, workflow_id, version)
            if workflow['status'] == 'retired':
                raise ValueError('workflow_retired')
            self._check_dependencies(workflow)
            row = self.store.db.execute('SELECT * FROM tool_workflow_runs WHERE run_id=?', (run_id,)).fetchone()
            if not row or (row['workflow_id'], row['version'], row['owner_member_id'], row['status']) != (workflow_id, version, owner, 'success'):
                raise ValueError('workflow_verification_run_invalid')
            payload = json.loads(row['payload'])
            if not self._steps_verified(owner, workflow, row):
                raise ValueError('workflow_step_execution_unverified')
            if not payload.get('verification_only') or payload.get('output') != expected_output:
                raise ValueError('workflow_verification_output_mismatch')
            if not isinstance(evidence_ids, list) or not evidence_ids:
                raise ValueError('workflow_verification_evidence_required')
            for evidence in evidence_ids:
                if self.store.verify_evidence(evidence, actor_member_id=owner)['status'] != 'valid':
                    raise ValueError('workflow_verification_evidence_invalid')
            existing = self.store.db.execute('SELECT * FROM tool_workflow_verifications WHERE run_id=?', (run_id,)).fetchone()
            encoded = json.dumps(evidence_ids)
            if existing:
                if existing['evidence_ids'] != encoded:
                    raise ValueError('workflow_verification_conflict')
                if self._independent_proofs(owner, workflow_id, version) < 2 and workflow['status'] == 'active':
                    self.store.db.execute("UPDATE tool_workflows SET status='candidate' WHERE workflow_id=? AND version=?", (workflow_id, version))
                    self.store.append_event(workflow_id, 'workflow_verification_stale', {'version': version}, 'workflow-proof-stale:' + uuid.uuid4().hex)
                return self.get(owner, workflow_id, version)
            self.store.db.execute('INSERT INTO tool_workflow_verifications VALUES (?,?,?,?)', ('workflow_verify_' + uuid.uuid4().hex, run_id, encoded, time.time()))
            # Composition admission requires distinct tasks and non-overlapping evidence.
            admission = 'active' if self._independent_proofs(owner, workflow_id, version) >= 2 else 'candidate'
            self.store.db.execute('UPDATE tool_workflows SET status=? WHERE workflow_id=? AND version=?', (admission, workflow_id, version))
            self.store.append_event(workflow_id, 'workflow_verified', {'version': version, 'run_id': run_id, 'evidence_ids': evidence_ids}, 'workflow-verify:' + run_id)
            return self.get(owner, workflow_id, version)
