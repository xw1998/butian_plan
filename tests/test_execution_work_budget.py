import pytest
from test_tools import make_service, propose, MANIFEST, execute_in_task, install_launch_failure_runner


def test_retained_work_budget_blocks_new_launch_without_deleting(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_TOOL_WORK_BUDGET_BYTES', '32')
    service = make_service(tmp_path)
    install_launch_failure_runner(service, 'budget admission must reject before dispatch')
    try:
        tool = propose(service)['tool_id']
        service.add_tool_version('m1', tool, "print('done')", {**MANIFEST, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py']})
        root = service.home / 'tool_execution_work'
        root.mkdir()
        asset = root / 'retained.bin'
        asset.write_bytes(b'x' * 64)
        with pytest.raises(ValueError, match='tool_work_budget_exhausted'):
            execute_in_task(service, 'm1', tool, 1, 'input', verification_only=True)
        assert asset.read_bytes() == b'x' * 64
    finally:
        service.close()

@pytest.mark.parametrize('value', ['-1', '1.5', 'NaN', 'Infinity', '', ' 32', 'true'])
def test_invalid_work_budget_blocks_launch(tmp_path, monkeypatch, value):
    monkeypatch.setenv('BUTIAN_TOOL_WORK_BUDGET_BYTES', value)
    service = make_service(tmp_path)
    install_launch_failure_runner(service, 'budget admission must reject before dispatch')
    try:
        tool = propose(service)['tool_id']
        service.add_tool_version('m1', tool, "print('done')", {**MANIFEST, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py']})
        with pytest.raises(ValueError, match='tool_work_budget_invalid'):
            execute_in_task(service, 'm1', tool, 1, 'input', verification_only=True)
        assert not (service.home / 'tool_execution_work').exists()
    finally:
        service.close()


def test_completed_execution_over_budget_keeps_artifact_and_audit(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    monkeypatch.setenv('BUTIAN_TOOL_WORK_BUDGET_BYTES', '32')
    service = make_service(tmp_path)
    try:
        tool = propose(service)['tool_id']
        source = "from pathlib import Path\nPath('asset.bin').write_bytes(b'x' * 64)\nprint('done')"
        service.add_tool_version('m1', tool, source, {**MANIFEST, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py']})
        result = execute_in_task(service, 'm1', tool, 1, 'input', verification_only=True)
        assert result['status'] == 'failed'
        assert result['termination_reason'] == 'tool_work_budget_exhausted'
        row = service.store.db.execute('SELECT isolation_capabilities FROM tool_executions WHERE execution_id=?', (result['execution_id'],)).fetchone()
        caps = json.loads(row['isolation_capabilities'])
        assert caps['work_bytes'] == 64
        assert caps['work_budget_status'] == 'exceeded'
        assert caps['file_size_limit'] is False
        assert (Path(caps['cwd']) / 'asset.bin').read_bytes() == b'x' * 64
        with pytest.raises(ValueError, match='tool_work_budget_exhausted'):
            execute_in_task(service, 'm1', tool, 1, 'input', verification_only=True)
    finally:
        service.close()


def test_work_budget_exhausted_during_admission_never_spawns(tmp_path, monkeypatch):
    import tempfile
    import subprocess
    monkeypatch.setenv('BUTIAN_TOOL_WORK_BUDGET_BYTES', '32')
    service = make_service(tmp_path)
    install_launch_failure_runner(service, 'budget admission must reject before dispatch')
    try:
        tool = propose(service)['tool_id']
        service.add_tool_version('m1', tool, "print('done')", {**MANIFEST, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py']})
        original = tempfile.mkdtemp
        def occupy_after_initial_check(*args, **kwargs):
            work = original(*args, **kwargs)
            (service.home / 'tool_execution_work' / 'concurrent-retained.bin').write_bytes(b'x' * 64)
            return work
        monkeypatch.setattr(tempfile, 'mkdtemp', occupy_after_initial_check)
        monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('exhausted work budget reached Popen'))
        result = execute_in_task(service, 'm1', tool, 1, 'input', verification_only=True)
        assert result['status'] == 'failed'
        assert 'tool_work_budget_exhausted' in result['stderr']
        assert result['exit_code'] is None
    finally:
        service.close()
