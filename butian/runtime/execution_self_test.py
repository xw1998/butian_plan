"""Explicitly requested process-control verification. Never run on discovery."""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from .execution_job import OwnedProcess


def verify_process_control(owner, work_root: Path) -> dict:
    """Only an operator in a permitted environment may invoke this routine.

    A refusal is latched; there is no alternate termination API or retry.
    The target and its descendant naturally expire after three seconds.
    """
    work_root = Path(work_root)
    if not work_root.is_dir() or work_root.is_symlink():
        raise ValueError('executor_self_test_directory_invalid')
    directory = Path(tempfile.mkdtemp(prefix='executor-self-test-', dir=work_root))
    descendant = "import time; time.sleep(3)"
    target = ('import subprocess,sys,time; '
              f'subprocess.Popen([sys.executable,"-c",{descendant!r}]); '
              'print("ready",flush=True); time.sleep(3)')
    process = None
    stage = 'launch'
    output = []
    try:
        process = OwnedProcess((sys.executable, '-I', '-u', '-c', target), str(directory), b'')
        stage = 'release'
        process.release()
        # Wait for the descendant to exist; bounded readiness is required so a
        # stopped bootstrap cannot masquerade as a tested process-tree stop.
        import threading
        ready = threading.Event()
        stage = 'readiness'
        def read_ready():
            output.append(process.process.stdout.readline(64))
            ready.set()
        reader = threading.Thread(target=read_ready, daemon=True)
        reader.start()
        if not ready.wait(2) or output != [b'ready\r\n'] and output != [b'ready\n']:
            raise RuntimeError('executor_self_test_readiness_unconfirmed')
        stage = 'descendant_membership'
        if process.job.active_count() < 3:
            raise RuntimeError('executor_self_test_descendant_unconfirmed')
        before = time.monotonic()
        stage = 'stop'
        process.stop()
        stage = 'exit_confirmation'
        deadline = before + 1
        while time.monotonic() < deadline and not process.confirmed_exit():
            ready.wait(0.01) if not ready.is_set() else time.sleep(0.01)
        if not process.confirmed_exit():
            raise RuntimeError('executor_self_test_stop_unconfirmed')
        owner.record_self_test(True, source='explicit_job_descendant_stop_test')
        return {'verified': True, 'executor_id': owner.executor_id, 'instance_id': owner.instance_id,
                'evidence': 'job_active_count_and_exit', 'artifacts': str(directory)}
    except (OSError, RuntimeError, ValueError) as exc:
        owner.record_self_test(False, source='explicit_self_test_failed')
        return {'verified': False, 'executor_id': owner.executor_id, 'instance_id': owner.instance_id,
                'error_type': type(exc).__name__, 'error_code': getattr(exc, 'winerror', getattr(exc, 'errno', None)),
                'stage': stage,
                'reason': str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith('executor_self_test_') else 'executor_self_test_operation_failed',
                'readiness_hex': [value.hex() for value in output],
                'artifacts': str(directory)}
    finally:
        if process is not None:
            # On a denied stop, only wait for the bounded test process to expire.
            process.process.wait(timeout=6)
            # Only the fixed bootstrap and fixed self-test run here. Preserve
            # their bounded startup diagnostics, never user command output.
            diagnostic = process.process.stderr.read(4096)
            (directory / 'bootstrap-stderr.txt').write_bytes(diagnostic)
            if process.confirmed_exit():
                process.close()
