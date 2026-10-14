import sys
from types import SimpleNamespace

import pytest

from butian.adapters.local import LocalCommandProvider
from butian.runtime.execution_contract import CapabilityEvidence, ExecutionCapabilities, ExecutionObservation, REQUIRED_CONTROL
from butian.runtime.execution_permit import request_digest
from butian.runtime.provider_execution import provider_request_factory
from butian.runtime.provider_runtime import ProviderRuntime
from butian.runtime.service import ButianService


def setup_business(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    service.add_member('m1', 'member')
    session = service.store.create_session('solo', 'm1', None, {})
    task = service.store.create_task(session, 'provider task', assignee_id='m1')
    service.store.db.execute("UPDATE tasks SET status='running' WHERE task_id=?", (task,))
    caps = ExecutionCapabilities('owner', 'instance', {
        name: CapabilityEvidence('supported', 'fixture_only', 1, 'instance') for name in REQUIRED_CONTROL})
    runner = SimpleNamespace(adapter=SimpleNamespace(capabilities=lambda: caps))
    provider = LocalCommandProvider([sys.executable, '-c', 'print(1)'], execution_runner=runner,
        execution_request_factory=provider_request_factory(service.store, tmp_path, runner))
    service.providers.register(provider)
    started = service.provider_runtime.start('m1', 'prompt', task_id=task, provider_id='local-command')
    remote = service.store.provider_session(started['provider_session_id'])['remote_session_id']
    return service, provider, runner, task, remote, started


def test_provider_permit_binds_source_input_task_and_revision(tmp_path, monkeypatch):
    service, provider, runner, task, remote, _ = setup_business(tmp_path, monkeypatch)
    try:
        request, authorized = provider.execution_request_factory(remote, 'prompt', provider.command, 10)
        assert request_digest(request) == request.request_sha256
        assert request.permit['task_id'] == task
        assert request.permit['member_id'] == 'm1'
        assert request.stdin == b'prompt'
        assert authorized()
        service.store.db.execute('UPDATE tasks SET revision=revision+1 WHERE task_id=?', (task,))
        assert not authorized()
        with pytest.raises(ValueError, match='provider_execution_task_unverified'):
            provider.execution_request_factory(remote, 'prompt', provider.command, 10)
    finally:
        service.close()


def test_provider_unified_business_path_never_uses_local_popen(tmp_path, monkeypatch):
    service, provider, runner, _, _, started = setup_business(tmp_path, monkeypatch)
    def run(request, *, authorization_valid):
        assert not service.store.db.in_transaction
        assert authorization_valid()
        return ExecutionObservation(request.execution_id, 'owner', 'instance', 1, 'success',
                                    exit_code=0, tree_exit_confirmed=True, metadata={'stdout': 'owned output'})
    runner.run = run
    monkeypatch.setattr('subprocess.Popen',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('local fallback')))
    try:
        result = service.provider_runtime.consume(started['provider_session_id'], provider, 'prompt')
        assert result['status'] == 'completed'
        assert result['text'] == 'owned output'
        assert provider._executor_sessions == {}
    finally:
        service.close()


def test_provider_cancelling_session_never_creates_execution(tmp_path, monkeypatch):
    service, provider, _, _, remote, started = setup_business(tmp_path, monkeypatch)
    try:
        service.store.db.execute("UPDATE provider_sessions SET status='cancelling' WHERE provider_session_id=?", (started['provider_session_id'],))
        with pytest.raises(ValueError, match='provider_execution_session_unverified'):
            provider.execution_request_factory(remote, 'prompt', provider.command, 10)
        assert not (tmp_path / 'provider_execution_work').exists()
    finally:
        service.close()


def test_provider_member_policy_revocation_invalidates_permission(tmp_path, monkeypatch):
    service, provider, _, _, remote, _ = setup_business(tmp_path, monkeypatch)
    try:
        _, authorized = provider.execution_request_factory(remote, 'prompt', provider.command, 10)
        service.store.db.execute("UPDATE members SET tool_policy=? WHERE member_id='m1'",
                                 ('{"default":"deny","permissions":[]}',))
        assert not authorized()
        with pytest.raises(ValueError, match='provider_execution_authorization_denied'):
            provider.execution_request_factory(remote, 'prompt', provider.command, 10)
    finally:
        service.close()


def test_provider_durable_precancel_is_not_unknown_or_retained():
    request = SimpleNamespace(execution_id='pre-cancelled')
    class Runner:
        def run(self, *args, **kwargs):
            from butian.runtime.execution_dispatch import ExecutionNotStarted
            raise ExecutionNotStarted('execution_cancelled_before_dispatch')
    provider = LocalCommandProvider(['unused'], execution_runner=Runner(),
        execution_request_factory=lambda *args: (request, lambda: True))
    events = list(provider.stream('session', 'prompt'))
    assert events[-1].status == 'cancelled'
    assert events[-1].reason == 'provider_execution_not_started'
    assert provider._executor_sessions == {}


def test_provider_without_executor_refuses_to_launch(monkeypatch):
    monkeypatch.setattr('subprocess.Popen',
        lambda *a, **k: (_ for _ in ()).throw(AssertionError('uncontrolled launch')))
    provider = LocalCommandProvider(['unused'])
    events = list(provider.stream('session', 'prompt'))
    assert len(events) == 1
    assert events[0].status == 'failed'
    assert events[0].reason == 'execution_backend_required'


def test_tool_without_executor_refuses_before_launch(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        with pytest.raises(ValueError, match='execution_backend_required'):
            service.tools.execute('member', 'tool', 1)
    finally:
        service.close()


def test_provider_cancel_waits_for_confirmed_owner_exit():
    import sqlite3
    from butian.runtime.execution_dispatch import ExecutionDispatch
    from butian.runtime.execution_runner import ExecutionRunner
    class Adapter:
        def capabilities(self):
            return ExecutionCapabilities('owner', 'instance', {})
        def cancel(self, execution_id, *, reason):
            return ExecutionObservation(execution_id, 'owner', 'instance', 0, 'cancelling', reason=reason)
        def inspect(self, execution_id):
            return ExecutionObservation(execution_id, 'owner', 'instance', 1, 'cancelled', reason='provider_cancelled', tree_exit_confirmed=True)
    db = sqlite3.connect(':memory:', isolation_level=None)
    runner = ExecutionRunner(ExecutionDispatch(db, Adapter()))
    db.execute("INSERT INTO execution_dispatch (execution_id,request_sha256,executor_id,instance_id,delivery_state,last_sequence,last_state,updated_at) VALUES ('e1',?,'owner','instance','prepared',-1,'accepted',0)", ('a'*64,))
    provider = LocalCommandProvider(['unused'], execution_runner=runner)
    provider._executor_sessions['session'] = 'e1'
    assert provider.cancel('session').status == 'cancelled'
    db.close()

def test_local_team_planning_has_persisted_task_permission(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    for member in ('dispatcher', 'worker'):
        service.add_member(member, member, provider_id='local-command')
    captured = []
    caps = ExecutionCapabilities('owner', 'instance', {
        name: CapabilityEvidence('supported', 'fixture_only', 1, 'instance') for name in REQUIRED_CONTROL})
    runner = SimpleNamespace(adapter=SimpleNamespace(capabilities=lambda: caps))
    def run(request, *, authorization_valid):
        assert authorization_valid()
        planning = service.store.db.execute('SELECT * FROM planning_executions WHERE planning_id=?', (request.permit['task_id'],)).fetchone()
        assert planning is not None
        assert planning['status'] == 'running'
        assert not service.store.list_tasks()
        captured.append(request.permit)
        return ExecutionObservation(request.execution_id, 'owner', 'instance', 1, 'success',
            exit_code=0, tree_exit_confirmed=True, metadata={'stdout': json.dumps({'subtasks': ['Verify evidence']})})
    runner.run = run
    provider = LocalCommandProvider(['unused'], execution_runner=runner,
        execution_request_factory=provider_request_factory(service.store, service.home, runner))
    service.providers.register(provider)
    try:
        result = service.submit_goal('Verify measurement', mode='team', member_id='dispatcher',
            member_ids=['worker'], problem={'acceptance_criteria': ['Evidence retained']})
        assert result['children'][0]['instruction'] == 'Verify evidence'
        assert captured[0]['member_id'] == 'dispatcher'
    finally:
        service.close()

def test_planning_permission_is_revocable_and_input_bound(tmp_path, monkeypatch):
    from butian.runtime.planning_execution import create, bind, finish
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    service.add_member('dispatcher', 'Dispatcher', provider_id='local-command')
    caps = ExecutionCapabilities('owner', 'instance', {
        name: CapabilityEvidence('supported', 'fixture_only', 1, 'instance') for name in REQUIRED_CONTROL})
    runner = SimpleNamespace(adapter=SimpleNamespace(capabilities=lambda: caps))
    factory = provider_request_factory(service.store, service.home, runner)
    try:
        planning = create(service.store, 'dispatcher', 1)
        session = service.store.create_provider_session('dispatcher', 'local-command', remote_session_id='planning-remote', prompt='bound prompt')
        bind(service.store, planning, session)
        with pytest.raises(ValueError, match='provider_execution_planning_unverified'):
            factory('planning-remote', 'altered prompt', ['unused'], 10)
        request, authorized = factory('planning-remote', 'bound prompt', ['unused'], 10)
        assert request.permit['task_id'] == planning
        assert authorized()
        service.store.db.execute('UPDATE provider_sessions SET prompt=? WHERE provider_session_id=?', ('changed prompt', session))
        assert not authorized()
        with pytest.raises(ValueError, match='provider_execution_planning_unverified'):
            factory('planning-remote', 'bound prompt', ['unused'], 10)
        service.store.db.execute('UPDATE provider_sessions SET prompt=? WHERE provider_session_id=?', ('bound prompt', session))
        assert authorized()
        finish(service.store, planning, 'revoked')
        assert not authorized()
        with pytest.raises(ValueError, match='provider_execution_planning_unverified'):
            factory('planning-remote', 'bound prompt', ['unused'], 10)
        assert not service.store.list_tasks()
        assert service.store.db.execute('SELECT status FROM planning_executions WHERE planning_id=?', (planning,)).fetchone()[0] == 'revoked'
    finally:
        service.close()

def test_unbound_taskless_provider_cannot_borrow_planning_permission(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    service.add_member('m', 'Member')
    service.store.create_provider_session('m', 'local-command', remote_session_id='unbound', prompt='prompt')
    try:
        factory = provider_request_factory(service.store, service.home, SimpleNamespace())
        with pytest.raises(ValueError, match='provider_execution_planning_unverified'):
            factory('unbound', 'prompt', ['unused'], 10)
    finally:
        service.close()

def test_planning_restart_revokes_stale_permission_without_replay(tmp_path, monkeypatch):
    from butian.runtime.planning_execution import create, bind
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    service.add_member('m', 'Member')
    planning = create(service.store, 'm', 1)
    session = service.store.create_provider_session('m', 'local-command', remote_session_id='lost-planning', prompt='prompt')
    bind(service.store, planning, session)
    service.close()
    service = ButianService(tmp_path)
    try:
        assert service.startup_recovery['planning_executions'] == [planning]
        factory = provider_request_factory(service.store, service.home, SimpleNamespace())
        with pytest.raises(ValueError, match='provider_execution_planning_unverified'):
            factory('lost-planning', 'prompt', ['unused'], 10)
        row = service.store.db.execute('SELECT status FROM planning_executions WHERE planning_id=?', (planning,)).fetchone()
        assert row[0] == 'unknown'
        assert not service.store.list_tasks()
        event = service.store.db.execute("SELECT payload FROM events WHERE aggregate_id=? AND event_type='planning_owner_lost'", (planning,)).fetchone()
        assert __import__('json').loads(event[0])['tree_exit_confirmed'] is False
    finally:
        service.close()


def test_invalid_planning_output_persists_failed_without_business_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    for member in ('dispatcher', 'worker'):
        service.add_member(member, member, provider_id='local-command')
    caps = ExecutionCapabilities('owner', 'instance', {
        name: CapabilityEvidence('supported', 'fixture_only', 1, 'instance') for name in REQUIRED_CONTROL})
    def run(request, *, authorization_valid):
        assert authorization_valid()
        return ExecutionObservation(request.execution_id, 'owner', 'instance', 1, 'success',
            exit_code=0, tree_exit_confirmed=True, metadata={'stdout': 'invalid plan'})
    runner = SimpleNamespace(adapter=SimpleNamespace(capabilities=lambda: caps), run=run)
    service.providers.register(LocalCommandProvider(['unused'], execution_runner=runner,
        execution_request_factory=provider_request_factory(service.store, service.home, runner)))
    try:
        with pytest.raises(ValueError, match='invalid_generated_team_plan'):
            service.submit_goal('Verify measurement', mode='team', member_id='dispatcher',
                member_ids=['worker'], problem={'acceptance_criteria': ['Evidence retained']})
        assert not service.store.list_tasks()
        rows = service.store.db.execute('SELECT status FROM planning_executions').fetchall()
        assert [row[0] for row in rows] == ['failed']
        assert service.store.db.execute("SELECT COUNT(*) FROM events WHERE event_type='planning_failed'").fetchone()[0] == 1
    finally:
        service.close()
