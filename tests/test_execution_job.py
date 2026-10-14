import json

from butian.runtime import execution_job


def test_bootstrap_and_command_keep_windows_system_root(monkeypatch, tmp_path):
    monkeypatch.setenv('SystemRoot', 'C:\\Windows')
    monkeypatch.setenv('EXECUTOR_TEST_SECRET', 'must-not-propagate')
    calls = []
    class Job:
        def assign(self, process):
            pass
        def close(self):
            pass
    class Process:
        pass
    def spawn(*args, **kwargs):
        calls.append(kwargs)
        return Process()
    monkeypatch.setattr(execution_job, 'WindowsJob', Job)
    monkeypatch.setattr(execution_job.subprocess, 'Popen', spawn)
    owned = execution_job.OwnedProcess(('python',), str(tmp_path), b'')
    environment = calls[0]['env']
    assert environment['SystemRoot'] == 'C:\\Windows'
    assert 'EXECUTOR_TEST_SECRET' not in environment
    assert json.loads(owned.header)['env'] == environment

def test_job_close_failure_preserves_handle_for_audit():
    import pytest
    from types import SimpleNamespace
    job = execution_job.WindowsJob.__new__(execution_job.WindowsJob)
    job.handle = 123
    job.api = SimpleNamespace(CloseHandle=lambda handle: False)
    with pytest.raises(OSError, match='executor_job_close_failed'):
        job.close()
    assert job.handle == 123

def test_large_header_is_prepared_before_small_release_gate():
    from types import SimpleNamespace
    calls = []
    class Pipe:
        def write(self, data): calls.append(data)
        def flush(self): pass
    owned = execution_job.OwnedProcess.__new__(execution_job.OwnedProcess)
    owned.process = SimpleNamespace(stdin=Pipe())
    owned.header = b'{"argv":[]}' + b' ' * 70000 + b'\n'
    owned.prepare_gate()
    assert calls == [owned.header[:-1]]
    owned.release_gate()
    assert calls == [owned.header[:-1], b'\n']

def test_bootstrap_eof_after_json_never_starts_command(monkeypatch):
    import builtins
    import pytest
    from types import SimpleNamespace
    data = iter(b'{"argv":["forbidden"],"cwd":".","env":{}}')
    fake_os = SimpleNamespace(name='posix')
    def read(*args):
        try: return bytes([next(data)])
        except StopIteration: return b''
    fake_os.read = read
    fake_process = SimpleNamespace(Popen=lambda *args, **kwargs: pytest.fail('EOF must not release user code'))
    original_import = builtins.__import__
    def importing(name, *args, **kwargs):
        return {'os': fake_os, 'subprocess': fake_process}.get(name) or original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', importing)
    with pytest.raises(SystemExit) as result:
        exec(execution_job._BOOTSTRAP, {})
    assert result.value.code == 125

def test_assignment_failure_keeps_unconfirmed_bootstrap_owned(monkeypatch, tmp_path):
    import subprocess
    import pytest
    from types import SimpleNamespace
    calls = []
    class Job:
        def assign(self, process): raise PermissionError('assignment denied')
        def close(self): calls.append('close')
    class Process:
        stdin = SimpleNamespace(close=lambda: calls.append('eof'))
        def wait(self, timeout): raise subprocess.TimeoutExpired('bootstrap', timeout)
        def poll(self): return None
    monkeypatch.setattr(execution_job, 'WindowsJob', Job)
    monkeypatch.setattr(execution_job.subprocess, 'Popen', lambda *a, **k: Process())
    with pytest.raises(PermissionError) as result:
        execution_job.OwnedProcess(('python',), str(tmp_path), b'')
    assert result.value.owned_process.process.poll() is None
    assert calls == ['eof']


def test_constructor_cleanup_failure_preserves_original_error_and_owner(monkeypatch, tmp_path):
    import pytest
    original = PermissionError('spawn denied')
    class Job:
        def active_count(self): return 0
        def close(self): raise OSError('close denied')
    monkeypatch.setattr(execution_job, 'WindowsJob', Job)
    def spawn(*args, **kwargs): raise original
    monkeypatch.setattr(execution_job.subprocess, 'Popen', spawn)
    with pytest.raises(PermissionError) as result:
        execution_job.OwnedProcess(('python',), str(tmp_path), b'')
    assert result.value is original
    assert result.value.owned_process.process is None
    assert result.value.gate_cleanup_error == 'OSError'

def test_job_configuration_cleanup_preserves_error_and_owner(monkeypatch, tmp_path):
    import pytest
    from types import SimpleNamespace
    class Operation:
        def __init__(self, value): self.value = value
        def __call__(self, *args): return self.value
    api = SimpleNamespace(**{name: Operation(value) for name, value in {
        'CreateJobObjectW': 123, 'SetInformationJobObject': False,
        'AssignProcessToJobObject': True, 'TerminateJobObject': True,
        'QueryInformationJobObject': True, 'CloseHandle': False,
    }.items()})
    monkeypatch.setattr(execution_job.ctypes, 'WinDLL', lambda *a, **k: api)
    monkeypatch.setattr(execution_job.ctypes, 'get_last_error', lambda: 5)
    monkeypatch.setattr(execution_job.subprocess, 'Popen', lambda *a, **k: pytest.fail('configuration failure must prevent spawn'))
    with pytest.raises(OSError, match='executor_job_configure_failed') as result:
        execution_job.OwnedProcess(('python',), str(tmp_path), b'')
    assert result.value.errno == 5
    assert result.value.gate_cleanup_error == 'OSError'
    assert result.value.owned_process.process is None
    assert result.value.owned_process.job.handle == 123


def test_confirmed_owner_close_releases_all_pipes():
    from types import SimpleNamespace
    calls = []
    owned = execution_job.OwnedProcess.__new__(execution_job.OwnedProcess)
    class Pipe:
        def __init__(self, name): self.name = name
        def close(self): calls.append(self.name)
    owned.process = SimpleNamespace(stdin=Pipe('stdin'), stdout=Pipe('stdout'), stderr=Pipe('stderr'))
    owned.job = SimpleNamespace(close=lambda: calls.append('job'))
    owned.close()
    assert calls == ['stdin', 'stdout', 'stderr', 'job']


def test_pipe_close_failure_preserves_job_for_retry():
    import pytest
    from types import SimpleNamespace
    calls = []
    class Pipe:
        def close(self): raise OSError('pipe close denied')
    owned = execution_job.OwnedProcess.__new__(execution_job.OwnedProcess)
    owned.process = SimpleNamespace(stdin=Pipe(), stdout=None, stderr=None)
    owned.job = SimpleNamespace(close=lambda: calls.append('job'))
    with pytest.raises(OSError, match='pipe close denied'):
        owned.close()
    assert calls == []
