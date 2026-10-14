from types import SimpleNamespace
from butian.adapters.local import LocalCommandProvider
from butian.runtime.execution_contract import ExecutionObservation

def test_timeout_stop_denied_returns_unknown_and_keeps_process(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('provider must not create or adopt a local process')
    monkeypatch.setattr('subprocess.Popen', forbidden)
    class Runner:
        def run(self, request, *, authorization_valid):
            assert authorization_valid()
            assert request.timeout == 0.01
            return ExecutionObservation('execution', 'owner', 'instance', 2, 'unknown',
                reason='process_tree_stop_permission_denied', tree_exit_confirmed=False)
    provider = LocalCommandProvider(['probe'], timeout=0.01, execution_runner=Runner(),
        execution_request_factory=lambda session, prompt, command, timeout:
            (SimpleNamespace(execution_id='execution', timeout=timeout), lambda: True))
    events = list(provider.stream('session', 'input'))
    assert events[-1].status == 'unknown'
    assert events[-1].reason == 'process_tree_stop_permission_denied'
    assert provider._executor_sessions['session'] == 'execution'
