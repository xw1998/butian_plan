"""Operator-only Windows acceptance; run outside an AI application's sandbox.

Uses only short-lived processes created here. Never retries a denied stop through
another API. Keeps databases, scripts and a credential-free JSON report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from butian.runtime.execution_contract import ExecutionRequest, TERMINAL_STATES
from butian.runtime.execution_owner import LocalExecutionOwner
from butian.runtime.execution_permit import capability_digest, request_digest
from butian.runtime.execution_protocol import MessageVerifier, canonical
from butian.runtime.execution_self_test import verify_process_control
from butian.runtime.execution_transport import LoopbackExecutionClient, LoopbackExecutionServer


def run(work):
    work.mkdir(parents=True, exist_ok=False)
    db = sqlite3.connect(work / 'owner.db', isolation_level=None, check_same_thread=False)
    client_db = sqlite3.connect(work / 'client.db', isolation_level=None)
    owner = LocalExecutionOwner(db, 'operator-acceptance', authorization_active=lambda *_: True)
    report = {'status': 'incomplete', 'source_revision': None, 'checks': [], 'artifacts': str(work)}
    # Content hashes let the maintainer reject stale acceptance evidence.
    report['source_hashes'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted((ROOT / 'butian').rglob('*.py'))}
    server = thread = None
    try:
        verified = verify_process_control(owner, work)
        report['checks'].append({'name': 'real_descendant_stop_self_test', **verified})
        if not verified['verified']:
            raise RuntimeError('operator_control_self_test_failed')
        key = secrets.token_bytes(32)  # Internal ephemeral identity; never printed or saved.
        server = LoopbackExecutionServer(('127.0.0.1', 0), owner,
            MessageVerifier(db, key, owner.executor_id, owner.instance_id))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = 'http://127.0.0.1:' + str(server.server_port)
        client = LoopbackExecutionClient(endpoint, MessageVerifier(client_db, key, owner.executor_id, owner.instance_id))
        caps = client.capabilities()
        caps.require()

        def make(name, code, timeout=8, output_limit=4096):
            folder = work / name
            folder.mkdir()
            source = (folder / 'command.py').resolve()
            source.write_text(code, encoding='utf-8')
            manifest = {'permissions': ['execute']}
            identity = 'acceptance:' + name
            permit = dict(member_id='operator', task_id='acceptance-task', problem_revision=1,
                tool_id='acceptance:' + name, version=1, source_path=str(source),
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                manifest=manifest, manifest_sha256=hashlib.sha256(canonical(manifest)).hexdigest(),
                input_sha256=hashlib.sha256(b'').hexdigest(), permissions=['execute'],
                authorization_id=identity, authorization_generation=0, expires_at=time.time()+30,
                executor_id=caps.executor_id, instance_id=caps.instance_id,
                capability_sha256=capability_digest(caps), timeout=timeout,
                output_limit=output_limit, work_budget_bytes=None)
            request = ExecutionRequest('operator_' + uuid.uuid4().hex, '0'*64,
                (sys.executable, '-I', '-u', str(source)), str(folder.resolve()), b'', timeout, output_limit, permit)
            return replace(request, request_sha256=request_digest(request))

        def start(request):
            client.authorize(request.permit['authorization_id'], 0, time.time()+30)
            return client.start(request)

        def finish(name, request, expected, reason=None, active_client=None):
            active_client = active_client or client
            deadline = time.monotonic()+12
            while time.monotonic() < deadline:
                observation = active_client.inspect(request.execution_id)
                if observation.state in TERMINAL_STATES:
                    break
                threading.Event().wait(0.02)
            else:
                raise RuntimeError(name + ':exit_deadline')
            report['checks'].append({'name': name, 'observation': asdict(observation)})
            if observation.state != expected or not observation.tree_exit_confirmed:
                raise RuntimeError(name + ':exit_not_confirmed')
            if reason is not None and observation.reason != reason:
                raise RuntimeError(name + ':reason_mismatch')
            return observation

        request = make('normal', "print('owned success')")
        start(request)
        assert finish('normal', request, 'success').metadata['stdout'].strip() == 'owned success'
        request = make('nonzero', 'raise SystemExit(7)')
        start(request)
        assert finish('nonzero', request, 'failed', 'nonzero_exit').exit_code == 7
        for name, stream in [('stdout_limit', 'stdout'), ('stderr_limit', 'stderr')]:
            request = make(name, f"import sys,time\nsys.{stream}.write('x'*32768)\nsys.{stream}.flush()\ntime.sleep(6)", output_limit=1024)
            start(request)
            finish(name, request, 'failed', 'output_limit')
        request = make('timeout', 'import time;time.sleep(6)', timeout=0.3)
        start(request)
        finish('timeout', request, 'failed', 'timeout')
        cancel_ready = work / 'cancel_ready'
        request = make('cancel', "import time\nfrom pathlib import Path\n"
                       f"Path({str(cancel_ready)!r}).write_text('ready')\ntime.sleep(6)")
        start(request)
        deadline = time.monotonic() + 2
        while not cancel_ready.exists() and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        if not cancel_ready.exists():
            raise RuntimeError('cancel_readiness_unconfirmed')
        client.cancel(request.execution_id, reason='operator_cancel')
        finish('cancel', request, 'cancelled', 'operator_cancel')
        # The readiness file is written after creating a real descendant.
        ready = work / 'tree_ready'
        child = 'import time;time.sleep(6)'
        code = ("import subprocess,sys,time\nfrom pathlib import Path\n"
                f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
                f"Path({str(ready)!r}).write_text('ready')\ntime.sleep(6)")
        request = make('descendant_cancel', code)
        start(request)
        deadline = time.monotonic()+2
        while not ready.exists() and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        if not ready.exists():
            raise RuntimeError('descendant_readiness_unconfirmed')
        with owner._lock:
            count = owner._active[request.execution_id]['process'].job.active_count()
        if count < 3:
            raise RuntimeError('descendant_job_membership_unconfirmed')
        client.cancel(request.execution_id, reason='operator_tree_cancel')
        finish('descendant_cancel', request, 'cancelled', 'operator_tree_cancel')
        request = make('revocation', 'import time;time.sleep(6)')
        start(request)
        client.revoke(request.permit['authorization_id'], 0)
        finish('revocation', request, 'cancelled', 'authorization_revoked')
        # A repeated accepted ID must never produce a second side effect.
        marker = work / 'side_effect.txt'
        request = make('reconnect_no_replay', f"from pathlib import Path\nwith Path({str(marker)!r}).open('a') as f:f.write('once\\n')")
        start(request)
        finish('reconnect_no_replay', request, 'success')
        restarted = LoopbackExecutionClient(endpoint, MessageVerifier(client_db, key, owner.executor_id, owner.instance_id))
        assert restarted.start(request).state == 'success'
        assert marker.read_text().splitlines() == ['once']
        report['checks'].append({'name': 'duplicate_start_no_replay', 'verified': True})
        # Losing the business connection stops lease renewal; natural exit is
        # deliberately later than the verified 10s control-lease stop window.
        request = make('control_disconnect', 'import time;time.sleep(13)', timeout=20)
        start(request)
        client.close()
        finish('control_disconnect', request, 'cancelled', 'control_lease_expired', restarted)
        report['status'] = 'passed'
    except Exception as exc:
        report['status'] = 'failed'
        report['error_type'] = type(exc).__name__
        # Our fixed reason strings contain no inputs or authentication material.
        report['reason'] = str(exc) if isinstance(exc, (RuntimeError, AssertionError)) else 'operator_acceptance_failed'
    finally:
        if server:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        try:
            owner.close()
        except Exception as exc:
            report['status'] = 'failed'
            report['shutdown'] = {'state': 'unknown', 'error_type': type(exc).__name__}
        if not owner._active:
            db.close()
        client_db.close()
        (work / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'status': report['status'], 'report': str(work / 'report.json')}, ensure_ascii=False))


    return 0 if report['status'] == 'passed' else 1

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operator-confirm', action='store_true', help='explicitly allow control of this test process tree')
    args = parser.parse_args()
    if not args.operator_confirm or os.name != 'nt':
        parser.error('Run on Windows outside the AI sandbox with --operator-confirm')
    folder = ROOT / '.agent_tmp' / ('executor-operator-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    raise SystemExit(run(folder))
