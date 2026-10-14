"""Real execution is enabled only by the explicitly confirmed operator entrypoint."""
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope='session')
def operator_backend(tmp_path_factory):
    if os.environ.get('BUTIAN_OPERATOR_ACCEPTANCE') != '1':
        yield None
        return
    sys.path.insert(0, str(Path(__file__).parent / 'acceptance'))
    from operator_backend import OperatorBackend
    root = tmp_path_factory.mktemp('operator-backend-parent') / 'executor'
    backend = OperatorBackend(root)
    yield backend
    backend.close()


@pytest.fixture(autouse=True)
def inject_operator_business_backend(request, monkeypatch, operator_backend):
    if operator_backend is None:
        return
    # Protocol, fault-injection and missing-backend tests own their dependencies.
    if request.node.path.name not in {
        'test_tools.py', 'test_execution_work_budget.py', 'test_execution_problem_revision.py',
        'test_tool_workflows.py', 'test_tool_evidence_gate.py', 'test_tool_interfaces.py',
        'test_team.py', 'test_team_domain_plan.py', 'test_provider_team_revision.py',
        'test_audit_boundaries.py', 'test_auto_learning.py', 'test_context.py',
        'test_problem_definition.py', 'test_dispatcher_goal_identity.py',
    }:
        return
    if request.node.originalname in {
        'test_unified_host_revocation_is_committed_before_remote_call',
        'test_host_revocation_lost_ack_is_durable_and_retryable',
        'test_host_revocation_wrong_receipt_remains_pending',
        'test_host_revocation_retry_refuses_different_executor_instance',
    }:
        return
    from butian.runtime.service import ButianService
    original = ButianService.__init__
    def initialized(service, *args, **kwargs):
        original(service, *args, **kwargs)
        if service.execution_runner is None:
            operator_backend.attach(service)
    monkeypatch.setattr(ButianService, '__init__', initialized)

@pytest.fixture
def operator_local_provider(operator_backend, tmp_path):
    """Real provider tests require the confirmed operator entrypoint."""
    if operator_backend is None:
        pytest.skip('real_provider_acceptance_requires_release_operator_terminal')
    from butian.adapters.local import LocalCommandProvider
    from butian.runtime.provider_execution import provider_request_factory
    from butian.runtime.service import ButianService
    service = ButianService(tmp_path / 'provider-business')
    try:
        runner = operator_backend.attach(service)
        service.add_member('m', 'Provider acceptance member')
        session = service.store.create_session('solo', 'm', None, {})
        task = service.store.create_task(session, 'Provider acceptance task', assignee_id='m')
        service.store.db.execute("UPDATE tasks SET status='running' WHERE task_id=?", (task,))
        def create(command, prompt):
            remote_id = 'operator-provider-' + __import__('uuid').uuid4().hex
            session_id = service.store.create_provider_session('m', 'local-command', task_id=task, prompt=prompt, remote_session_id=remote_id)
            service.store.db.execute("UPDATE provider_sessions SET status='running' WHERE provider_session_id=?", (session_id,))
            provider = LocalCommandProvider(command, execution_runner=runner,
                execution_request_factory=provider_request_factory(service.store, service.home, runner))
            remote = service.store.provider_session(session_id)['remote_session_id']
            return service, provider, {'provider_session_id': session_id}, remote
        yield create
    finally:
        service.close()
