import subprocess
import pytest
from butian.runtime import tools

class FaultExecutionAdapter:
    """No OS calls: exercise the production dispatch and runner interfaces."""
    def __init__(self, after_start=None):
        self.after_start = after_start
        self.execution_id = None
        self.exit_confirmed = False
    def capabilities(self):
        from butian.runtime.execution_contract import CapabilityEvidence, ExecutionCapabilities, REQUIRED_CONTROL
        return ExecutionCapabilities('fault-owner', 'fault-instance', {
            name: CapabilityEvidence('supported', 'fault_fixture_only', 1, 'fault-instance')
            for name in REQUIRED_CONTROL})
    def authorize(self, *args): pass
    def start(self, request):
        self.execution_id = request.execution_id
        if self.after_start: self.after_start()
        return self.inspect(request.execution_id)
    def inspect(self, identity):
        from butian.runtime.execution_contract import ExecutionObservation
        return ExecutionObservation(identity, 'fault-owner', 'fault-instance',
            3 if self.exit_confirmed else 2, 'unknown', reason='stop_unconfirmed',
            tree_exit_confirmed=self.exit_confirmed,
            metadata={'error_type': 'PermissionError', 'error_code': None})
    def events(self, identity, after_sequence=-1):
        from butian.runtime.execution_contract import ExecutionObservation
        history = [ExecutionObservation(identity, 'fault-owner', 'fault-instance', 0, 'accepted'),
                   ExecutionObservation(identity, 'fault-owner', 'fault-instance', 1, 'running'),
                   ExecutionObservation(identity, 'fault-owner', 'fault-instance', 2, 'unknown',
                       reason='stop_unconfirmed', metadata={'error_type': 'PermissionError', 'error_code': None})]
        if self.exit_confirmed: history.append(self.inspect(identity))
        return [value for value in history if value.sequence > after_sequence]

def fault_runner(service, after_start=None):
    import sqlite3
    from butian.runtime.execution_dispatch import ExecutionDispatch
    from butian.runtime.execution_runner import ExecutionRunner
    db = sqlite3.connect(':memory:', isolation_level=None)
    adapter = FaultExecutionAdapter(after_start)
    runner = ExecutionRunner(ExecutionDispatch(db, adapter))
    service.tools.execution_runner = runner
    return runner, adapter, db


def test_tree_stop_permission_denied_never_tries_another_stop(monkeypatch):
    calls = []
    class Process:
        pid = 123
        def poll(self): return None
        def kill(self): calls.append('kill')
    monkeypatch.setattr(tools, '_windows_process_tree', lambda pid: {123, 456})
    def denied(*args, **kwargs):
        calls.append('taskkill')
        raise PermissionError('injected OS denial')
    monkeypatch.setattr(subprocess, 'run', denied)
    process = Process()
    assert tools._terminate_process_tree(process) is False
    assert tools._terminate_process_tree(process) is False
    assert calls == ['taskkill']


def test_tree_observation_unavailable_never_attempts_stop(monkeypatch):
    class Process:
        pid = 123
        def kill(self): pytest.fail('must not stop without tree observation')
    monkeypatch.setattr(tools, '_windows_process_tree', lambda pid: None)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('must not stop without tree observation'))
    assert tools._terminate_process_tree(Process()) is False

def test_unknown_execution_remains_visible_to_owner(tmp_path):
    from butian.runtime.service import ButianService
    service = ButianService(tmp_path)
    try:
        service.add_member('owner', 'Owner')
        service.store.db.execute("INSERT INTO tool_executions(execution_id,tool_id,version,actor_member_id,input_hash,dry_run,status,stdout,stderr,permissions,started_at,runtime_backend,isolation_level,isolation_capabilities,business_assertions) VALUES ('retained','missing-asset',1,'owner','hash',0,'unknown','','','[]',1,'python_host','host_process','{}','[]')")
        rows = service.tools.list_running_executions('owner')
        assert any(row['execution_id'] == 'retained' for row in rows)
        assert service.tools.list_running_executions('other') == []
    finally:
        service.close()

def test_unknown_process_retains_working_directory(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from test_tools import make_service, propose, MANIFEST, execute_in_task
    service = make_service(tmp_path)
    runner, adapter, db = fault_runner(service)
    try:
        tool = propose(service)['tool_id']
        service.add_tool_version('m1', tool, "print('done')", {**MANIFEST, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py']})
        monkeypatch.setattr(tools.subprocess, 'Popen', lambda *a, **k: pytest.fail('no local fallback'))
        result = execute_in_task(service, 'm1', tool, 1, 'input', verification_only=True)
        assert result['status'] == 'unknown'
        assert result['stop_error'] == {'code': None, 'type': 'PermissionError'}
        row = service.store.db.execute('SELECT isolation_capabilities FROM tool_executions WHERE execution_id=?', (result['execution_id'],)).fetchone()
        assert Path(json.loads(row[0])['cwd']).is_dir()
        assert result['execution_id'] in runner.unresolved()
        assert not service.tools.wait_for_executions(timeout=.01)
    finally:
        adapter.exit_confirmed = True
        assert service.tools.wait_for_executions(timeout=.1)
        service.close()
        db.close()

@pytest.mark.parametrize('error_type', [ValueError, RuntimeError, __import__('sqlite3').OperationalError])
def test_post_launch_failure_retains_unknown_process(tmp_path, monkeypatch, error_type):
    from test_tools import make_service, propose, MANIFEST, execute_in_task
    service = make_service(tmp_path)
    def fail_after_start(): raise error_type('injected post-dispatch failure')
    runner, adapter, db = fault_runner(service, fail_after_start)
    try:
        tool = propose(service)['tool_id']
        service.add_tool_version('m1', tool, "print('done')", {**MANIFEST, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py']})
        monkeypatch.setattr(tools.subprocess, 'Popen', lambda *a, **k: pytest.fail('no local fallback'))
        result = execute_in_task(service, 'm1', tool, 1, 'input', verification_only=True)
        assert adapter.execution_id == result['execution_id']
        assert result['status'] == 'unknown'
        assert result['termination_reason'] == 'execution_owner_exit_unconfirmed'
        assert result['execution_id'] in runner.unresolved()
        assert not service.tools.wait_for_executions(timeout=.01)
        adapter.exit_confirmed = True
        assert service.tools.wait_for_executions(timeout=.1)
    finally:
        adapter.exit_confirmed = True
        service.tools.wait_for_executions(timeout=.1)
        service.close()
        db.close()

def test_taskkill_nonzero_does_not_try_alternative_stop(monkeypatch):
    calls = []
    class Process:
        pid = 123
        def poll(self): return None
        def kill(self): calls.append('kill')
    monkeypatch.setattr(tools, '_windows_process_tree', lambda pid: {123, 456})
    def refused(*args, **kwargs):
        calls.append('taskkill')
        return subprocess.CompletedProcess(args[0], 5, '', 'Access denied')
    monkeypatch.setattr(subprocess, 'run', refused)
    process = Process()
    assert tools._terminate_process_tree(process) is False
    assert tools._terminate_process_tree(process) is False
    assert calls == ['taskkill']
    assert process._butian_stop_error_code == 5


@pytest.mark.parametrize('failure', [OSError('stop unavailable'), subprocess.TimeoutExpired('taskkill', 5)])
def test_stop_transport_failure_blocks_further_attempts(monkeypatch, failure):
    calls = []
    class Process:
        pid = 123
        def poll(self): return None
        def kill(self): calls.append('kill')
    monkeypatch.setattr(tools, '_windows_process_tree', lambda pid: {123, 456})
    def failed(*args, **kwargs):
        calls.append('taskkill')
        raise failure
    monkeypatch.setattr(subprocess, 'run', failed)
    process = Process()
    assert tools._terminate_process_tree(process) is False
    assert tools._terminate_process_tree(process) is False
    assert calls == ['taskkill']


def test_concurrent_stop_denial_attempts_once(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    calls = []
    entered, release = threading.Event(), threading.Event()
    class Process:
        pid = 123
        def poll(self): return None
        def kill(self): calls.append('kill')
    monkeypatch.setattr(tools, '_windows_process_tree', lambda pid: {123})
    def denied(*args, **kwargs):
        calls.append('taskkill')
        entered.set()
        assert release.wait(2)
        return subprocess.CompletedProcess(args[0], 5)
    monkeypatch.setattr(subprocess, 'run', denied)
    process = Process()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(tools._terminate_process_tree, process)
        assert entered.wait(2)
        second = pool.submit(tools._terminate_process_tree, process)
        release.set()
        assert first.result() is False
        assert second.result() is False
    assert calls == ['taskkill']


@pytest.mark.parametrize('recovery_io_failure', [False, True])
def test_persistent_database_failure_preserves_live_execution(tmp_path, monkeypatch, recovery_io_failure):
    import sqlite3
    from pathlib import Path
    from test_tools import make_service, propose, MANIFEST, execute_in_task
    service = make_service(tmp_path)
    original_db = service.store.db
    class UnavailableDB:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError('persistent database unavailable')
    def fail_after_start():
        service.store.db = UnavailableDB()
        raise sqlite3.OperationalError('persistent database unavailable')
    runner, adapter, db = fault_runner(service, fail_after_start)
    try:
        tool = propose(service)['tool_id']
        service.add_tool_version('m1', tool, "print('done')", {**MANIFEST, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py']})
        monkeypatch.setattr(tools.subprocess, 'Popen', lambda *a, **k: pytest.fail('no local fallback'))
        if recovery_io_failure:
            original_open = Path.open
            def deny_recovery(path, *args, **kwargs):
                if path.parent.name == 'execution_recovery':
                    raise PermissionError('injected recovery write denial')
                return original_open(path, *args, **kwargs)
            monkeypatch.setattr(Path, 'open', deny_recovery)
        result = execute_in_task(service, 'm1', tool, 1, 'input', verification_only=True)
        assert result['status'] == 'unknown'
        assert result['audit_status'] == 'persistence_failed'
        assert result['execution_id'] in runner.unresolved()
        assert not service.tools.wait_for_executions(timeout=.01)
        if recovery_io_failure:
            assert result['recovery_record'] is None
            assert result['recovery_error_type'] == 'PermissionError'
            assert service.tools._execution_recovery_snapshots[result['execution_id']]['audit_status'] == 'persistence_failed'
        else:
            import json
            fault = Path(result['recovery_record'])
            assert fault.is_file()
            saved = json.loads(fault.read_text(encoding='utf-8'))
            assert saved['execution_id'] == result['execution_id']
            assert saved['pid'] is None
            assert saved['status'] == 'unknown'
    finally:
        service.store.db = original_db
        adapter.exit_confirmed = True
        assert service.tools.wait_for_executions(timeout=.1)
        service.close()
        db.close()

def test_recovery_record_is_visible_after_restart_only_to_actor(tmp_path, monkeypatch):
    import json
    from butian.runtime.service import ButianService
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    service.add_member('owner', 'Owner')
    service.add_member('other', 'Other')
    directory = tmp_path / 'execution_recovery'
    directory.mkdir()
    record = {'execution_id': 'fault-only', 'tool_id': 'asset', 'version': 1, 'actor_member_id': 'owner', 'task_id': None, 'pid': 123, 'status': 'unknown', 'audit_status': 'persistence_failed', 'started_at': 1}
    (directory / 'fault-only.json').write_text(json.dumps(record), encoding='utf-8')
    service.close()
    service = ButianService(tmp_path)
    try:
        rows = service.tools.list_running_executions('owner')
        assert any(row['execution_id'] == 'fault-only' and row['audit_status'] == 'persistence_failed' for row in rows)
        assert service.tools.list_running_executions('other') == []
    finally:
        service.close()


def test_running_list_database_failure_never_claims_complete_empty(tmp_path, monkeypatch):
    import sqlite3
    from test_tools import make_service
    service = make_service(tmp_path)
    original_db = service.store.db
    class UnavailableDB:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError('persistent database unavailable')
    try:
        service.store.db = UnavailableDB()
        with pytest.raises(RuntimeError, match='execution_list_database_unavailable'):
            service.tools.list_running_executions('m1')
    finally:
        service.store.db = original_db
        service.close()


@pytest.mark.parametrize('started_at', ['yesterday', True, float('nan'), float('inf')])
def test_recovery_record_rejects_invalid_timestamp(tmp_path, started_at):
    import json
    from test_tools import make_service
    service = make_service(tmp_path)
    directory = tmp_path / 'execution_recovery'
    directory.mkdir()
    record = {'execution_id': 'invalid-time', 'actor_member_id': 'm1', 'status': 'unknown', 'started_at': started_at}
    (directory / 'invalid-time.json').write_text(json.dumps(record), encoding='utf-8')
    try:
        with pytest.raises(RuntimeError, match='execution_recovery_record_unreadable'):
            service.tools.list_running_executions('m1')
    finally:
        service.close()


def test_terminal_recovery_record_remains_visible_in_execution_history(tmp_path):
    import json
    from test_tools import make_service
    service = make_service(tmp_path)
    directory = tmp_path / 'execution_recovery'
    directory.mkdir()
    record = {'execution_id': 'failed-audit', 'actor_member_id': 'm1', 'tool_id': 'tool-a', 'version': 1, 'status': 'failed', 'started_at': 10, 'audit_status': 'persistence_failed'}
    (directory / 'failed-audit.json').write_text(json.dumps(record), encoding='utf-8')
    try:
        history = service.tools.list_executions('m1')
        assert any(row['execution_id'] == 'failed-audit' and row['audit_status'] == 'persistence_failed' for row in history)
        assert not service.tools.list_executions('m2')
        assert not service.tools.list_executions('m1', tool_id='other')
    finally:
        service.close()
