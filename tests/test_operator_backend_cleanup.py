"""Fault-only checks; never enable or instantiate the OS execution backend."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def backend_type():
    path = Path(__file__).parent / 'acceptance' / 'operator_backend.py'
    spec = importlib.util.spec_from_file_location('operator_cleanup_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.OperatorBackend


def test_close_failure_still_closes_listener_but_preserves_owner_ledger():
    calls = []
    backend = backend_type().__new__(backend_type())
    failure = RuntimeError('shutdown_incomplete_active_tool_executions')
    def owner_close():
        calls.append('owner')
        raise failure
    backend.owner = SimpleNamespace(close=owner_close)
    backend.server = SimpleNamespace(shutdown=lambda: calls.append('shutdown'), server_close=lambda: calls.append('listener'))
    backend.thread = SimpleNamespace(join=lambda timeout: calls.append('join'), is_alive=lambda: False)
    backend.client_db = SimpleNamespace(close=lambda: calls.append('client_db'))
    backend.db = SimpleNamespace(close=lambda: calls.append('owner_db'))
    with pytest.raises(RuntimeError) as result:
        backend.close()
    assert result.value is failure
    assert calls == ['owner', 'shutdown', 'listener', 'join', 'client_db']


def test_normal_close_releases_ledger_after_listener_exit():
    calls = []
    backend = backend_type().__new__(backend_type())
    backend.owner = SimpleNamespace(close=lambda: calls.append('owner'))
    backend.server = SimpleNamespace(shutdown=lambda: calls.append('shutdown'), server_close=lambda: calls.append('listener'))
    backend.thread = SimpleNamespace(join=lambda timeout: calls.append('join'), is_alive=lambda: False)
    backend.client_db = SimpleNamespace(close=lambda: calls.append('client_db'))
    backend.db = SimpleNamespace(close=lambda: calls.append('owner_db'))
    backend.close()
    assert calls == ['owner', 'shutdown', 'listener', 'join', 'client_db', 'owner_db']

def test_attach_binds_loaded_local_provider_without_starting_process(tmp_path):
    from butian.adapters.local import LocalCommandProvider
    from butian.runtime.service import ButianService
    service = ButianService(tmp_path)
    backend = backend_type().__new__(backend_type())
    backend.client = SimpleNamespace()
    provider = LocalCommandProvider(['unused'])
    service.providers.register(provider)
    try:
        runner = backend.attach(service)
        assert service.tools.execution_runner is runner
        assert provider.execution_runner is runner
        assert callable(provider.execution_request_factory)
    finally:
        service.execution_runner = None
        service.tools.execution_runner = None
        service.close()

def test_initialization_failure_closes_confirmed_owner_ledger(monkeypatch, tmp_path):
    cls = backend_type()
    module = __import__('sys').modules.get(cls.__module__)
    # The dynamically loaded class retains its own module globals.
    scope = cls.__init__.__globals__
    calls = []
    monkeypatch.setitem(scope, 'os', SimpleNamespace(name='nt', environ={'BUTIAN_OPERATOR_ACCEPTANCE': '1'}))
    monkeypatch.setitem(scope, 'sqlite3', SimpleNamespace(connect=lambda *a, **k: SimpleNamespace(close=lambda: calls.append('db')), Error=RuntimeError))
    monkeypatch.setitem(scope, 'LocalExecutionOwner', lambda *a, **k: SimpleNamespace(close=lambda: calls.append('owner')))
    def fail(*args):
        raise ValueError('self_test_fault')
    monkeypatch.setitem(scope, 'verify_process_control', fail)
    with pytest.raises(ValueError, match='self_test_fault'):
        cls(tmp_path / 'executor')
    assert calls == ['owner', 'db']
