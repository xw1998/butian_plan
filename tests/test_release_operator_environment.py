from pathlib import Path
from types import SimpleNamespace

from tests.acceptance.release_operator import sdk_environment


def test_sdk_connection_is_injected_without_mutating_other_checks():
    environment = {'BUTIAN_OPERATOR_ACCEPTANCE': '1', 'BUTIAN_TEST_PYTHON': 'python'}
    backend = SimpleNamespace(connection_path=Path('D:/acceptance/connection.json'))
    selected = sdk_environment(environment, backend)
    assert selected['BUTIAN_EXECUTOR_CONNECTION'] == str(backend.connection_path)
    assert selected['BUTIAN_OPERATOR_ACCEPTANCE'] == '1'
    assert 'BUTIAN_EXECUTOR_CONNECTION' not in environment
    assert selected is not environment


import json
import sqlite3
import pytest


@pytest.mark.parametrize('failure', [None, 'sdk', 'shutdown', 'sdk_database', 'shutdown_database'])
def test_release_entrypoint_owns_one_sdk_bridge_and_records_cleanup(tmp_path, monkeypatch, failure):
    from tests.acceptance import release_operator as entry
    from tests.acceptance import operator_backend as bridge
    calls = []
    class Backend:
        def __init__(self, root, *, operator_confirm):
            assert operator_confirm is True
            calls.append('create')
            self.connection_path = root / 'connection.json'
        def close(self):
            calls.append('close')
            if failure == 'shutdown_database':
                raise sqlite3.OperationalError('shutdown_database_fault')
            if failure == 'shutdown':
                raise RuntimeError('shutdown_unconfirmed')
    def run(command, **kwargs):
        env = kwargs['env']
        if command[1:3] == ['-m', 'pytest']:
            assert 'BUTIAN_EXECUTOR_CONNECTION' not in env
            xml = next(value.split('=', 1)[1] for value in command if value.startswith('--junitxml='))
            Path(xml).write_text('<testsuites><testsuite><testcase name="checked"/></testsuite></testsuites>')
        if command[0] == 'node':
            assert env['BUTIAN_EXECUTOR_CONNECTION'].endswith('connection.json')
            calls.append('sdk')
            if failure == 'sdk_database':
                raise sqlite3.OperationalError('sdk_database_fault')
            if failure == 'sdk':
                raise RuntimeError('sdk_fault')
        else:
            assert 'BUTIAN_EXECUTOR_CONNECTION' not in env
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(entry, 'ROOT', tmp_path)
    monkeypatch.setattr(entry.sys, 'argv', ['release_operator.py', '--operator-confirm'])
    monkeypatch.setattr(bridge, 'OperatorBackend', Backend)
    monkeypatch.setattr(entry.subprocess, 'run', run)
    monkeypatch.delenv('BUTIAN_EXECUTOR_CONNECTION', raising=False)
    result = entry.main()
    assert calls.count('create') == 1
    assert calls.count('close') == 1
    assert calls[-1] == 'close'
    report = json.loads(next(tmp_path.rglob('report.json')).read_text())
    assert result == (0 if failure is None else 1)
    assert report['status'] == ('passed' if failure is None else 'failed')
    assert calls.count('sdk') == (1 if failure in {'sdk', 'sdk_database'} else 8)
    assert report['checks'][-1]['name'] == 'sdk_executor_shutdown'

@pytest.mark.parametrize('version,available,error', [((3, 10), True, 'PythonVersionUnsupported'), ((3, 12), False, 'PytestUnavailable')])
def test_preflight_failure_does_not_start_operator(tmp_path, monkeypatch, version, available, error):
    from tests.acceptance import release_operator as entry
    monkeypatch.setattr(entry, 'ROOT', tmp_path)
    monkeypatch.setattr(entry.sys, 'argv', ['release_operator.py', '--operator-confirm'])
    monkeypatch.setattr(entry.sys, 'version_info', version)
    monkeypatch.setattr(entry.importlib.util, 'find_spec', lambda name: object() if available else None)
    def forbidden(*args, **kwargs):
        pytest.fail('preflight failure must not launch a process')
    monkeypatch.setattr(entry.subprocess, 'run', forbidden)
    assert entry.main() == 1
    report = json.loads(next(tmp_path.rglob('report.json')).read_text())
    assert report['status'] == 'failed'
    assert report['checks'][0]['error_type'] == error
