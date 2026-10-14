from dataclasses import replace

import pytest

from butian.runtime.execution_contract import CapabilityEvidence, ExecutionCapabilities, ExecutionObservation, ExecutionRequest, REQUIRED_CONTROL
from butian.runtime.execution_host import HostExecutionAdapter


def adapter(*, lost=False, verified=True):
    caps = ExecutionCapabilities('host', 'instance', {
        name: CapabilityEvidence('supported', 'native_self_test' if verified else 'implementation', 1, 'instance')
        for name in REQUIRED_CONTROL})
    calls = []
    observation = ExecutionObservation('e1', 'host', 'instance', 1, 'running')
    def start(request):
        calls.append(request.execution_id)
        if lost:
            raise ConnectionError('response lost')
        return observation
    value = HostExecutionAdapter(capabilities=lambda: caps, start=start,
                                 inspect=lambda _: observation, events=lambda *_: [],
                                 cancel=lambda *_: replace(observation, sequence=2, state='cancelling'),
                                 close=lambda: None)
    request = ExecutionRequest('e1', 'a' * 64, ('python',), '.', b'', 10, 1024, {})
    return value, calls, request


def test_lost_start_response_retries_inspect_without_second_start():
    value, calls, request = adapter(lost=True)
    with pytest.raises(ConnectionError):
        value.start(request)
    assert value.start(request).state == 'running'
    assert calls == ['e1']


def test_static_capability_claim_prevents_host_start():
    value, calls, request = adapter(verified=False)
    with pytest.raises(ValueError, match='execution_capabilities_missing'):
        value.start(request)
    assert calls == []


def test_same_id_different_request_cannot_execute():
    value, calls, request = adapter()
    value.start(request)
    with pytest.raises(ValueError, match='execution_id_request_conflict'):
        value.start(replace(request, request_sha256='b' * 64))
    assert calls == ['e1']


def test_cancel_acknowledgement_is_not_cancelled():
    value, _, request = adapter()
    value.start(request)
    assert value.cancel('e1', reason='user_requested').state == 'cancelling'

def test_missing_native_authorization_controls_fail_before_start():
    value, calls, request = adapter()
    with pytest.raises(ValueError, match='host_execution_authorization_control_unavailable'):
        value.authorize('business:e1', 0, 30)
    assert calls == []

def test_native_authorization_controls_are_delegated():
    value, calls, request = adapter()
    recorded = []
    value._authorize = lambda *args: recorded.append(('authorize', args))
    value._renew = lambda *args: recorded.append(('renew', args))
    value._revoke = lambda *args: recorded.append(('revoke', args))
    value.authorize('business:e1', 0, 30)
    value.renew('e1', 10)
    value.revoke('business:e1', 0)
    assert recorded == [('authorize', ('business:e1', 0, 30)), ('renew', ('e1', 10)), ('revoke', ('business:e1', 0))]


def test_native_observation_cannot_regress_running_state():
    value, _, request = adapter()
    value.start(request)
    value._inspect = lambda _: ExecutionObservation('e1', 'host', 'instance', 2, 'accepted')
    with pytest.raises(ValueError, match='host_execution_state_regression'):
        value.inspect('e1')

def test_native_unknown_can_confirm_exit_without_changing_result():
    value, _, request = adapter()
    value.start(request)
    unknown = ExecutionObservation('e1', 'host', 'instance', 2, 'unknown', reason='stop_unconfirmed')
    value._inspect = lambda _: unknown
    assert value.inspect('e1') == unknown
    confirmed = replace(unknown, sequence=3, tree_exit_confirmed=True)
    value._inspect = lambda _: confirmed
    assert value.inspect('e1') == confirmed
    value._inspect = lambda _: replace(confirmed, sequence=4, tree_exit_confirmed=False)
    with pytest.raises(ValueError, match='host_execution_terminal_conflict'):
        value.inspect('e1')

def test_host_restart_accepts_only_original_identity_lost_observation():
    value, calls, request = adapter()
    value.start(request)
    caps = ExecutionCapabilities('host', 'replacement', {
        name: CapabilityEvidence('supported', 'native_self_test', 1, 'replacement')
        for name in REQUIRED_CONTROL})
    value._capabilities = lambda: caps
    lost = ExecutionObservation('e1', 'host', 'instance', 2, 'unknown',
        reason='executor_instance_lost', metadata={'recovery_instance_id': 'replacement'})
    value._inspect = lambda _: lost
    assert value.inspect('e1') == lost
    value._events = lambda *_: [lost]
    assert value.events('e1', 1) == [lost]
    assert calls == ['e1']

def test_host_restart_cannot_accept_old_instance_success():
    value, _, request = adapter()
    value.start(request)
    caps = replace(value.capabilities(), instance_id='replacement')
    value._capabilities = lambda: caps
    value._inspect = lambda _: ExecutionObservation('e1', 'host', 'instance', 2,
        'success', exit_code=0, tree_exit_confirmed=True,
        metadata={'recovery_instance_id': 'replacement'})
    with pytest.raises(ValueError, match='host_execution_identity_mismatch'):
        value.inspect('e1')

def test_host_events_reject_terminal_state_rewrite():
    value, _, request = adapter()
    value.start(request)
    values = [ExecutionObservation('e1', 'host', 'instance', 2, 'unknown', reason='stop_unconfirmed'),
              ExecutionObservation('e1', 'host', 'instance', 3, 'success', exit_code=0, tree_exit_confirmed=True)]
    value._events = lambda *_: values
    with pytest.raises(ValueError, match='host_execution_terminal_conflict'):
        value.events('e1', 1)


def test_host_events_reject_running_to_accepted_regression():
    value, _, request = adapter()
    value.start(request)
    value._events = lambda *_: [ExecutionObservation('e1', 'host', 'instance', 2, 'accepted')]
    with pytest.raises(ValueError, match='host_execution_state_regression'):
        value.events('e1', 1)

def test_host_event_history_replay_preserves_current_observation():
    value, _, request = adapter()
    value.start(request)
    running = value.inspect('e1')
    value._events = lambda *_: [replace(running, sequence=0, state='accepted'), running]
    assert value.events('e1', -1)[-1] == running
    assert value.inspect('e1') == running


def test_invalid_host_event_batch_does_not_publish_partial_state():
    value, _, request = adapter()
    value.start(request)
    running = value.inspect('e1')
    value._events = lambda *_: [replace(running, sequence=2, state='unknown', reason='stop_unconfirmed'),
                               replace(running, sequence=3, state='success', exit_code=0, tree_exit_confirmed=True)]
    with pytest.raises(ValueError, match='host_execution_terminal_conflict'):
        value.events('e1', 1)
    assert value.inspect('e1') == running

def test_historical_batch_cannot_replace_cached_terminal_at_crossing():
    value, _, request = adapter()
    value.start(request)
    terminal = ExecutionObservation('e1', 'host', 'instance', 2, 'unknown', reason='stop_unconfirmed')
    value._inspect = lambda _: terminal
    value.inspect('e1')
    value._events = lambda *_: [
        ExecutionObservation('e1', 'host', 'instance', 1, 'running'),
        ExecutionObservation('e1', 'host', 'instance', 2, 'running'),
        ExecutionObservation('e1', 'host', 'instance', 3, 'success', exit_code=0, tree_exit_confirmed=True),
    ]
    with pytest.raises(ValueError, match='host_execution_event_conflict'):
        value.events('e1', 0)
    assert value._observations['e1'] == terminal
