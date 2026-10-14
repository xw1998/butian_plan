import sqlite3

import pytest

from butian.runtime.execution_owner import LocalExecutionOwner
from butian.runtime.execution_contract import ExecutionRequest


def test_owner_does_not_start_before_explicit_capability_verification():
    db = sqlite3.connect(':memory:', isolation_level=None)
    calls = []
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True,
                                process_factory=lambda *args: calls.append(args))
    request = ExecutionRequest('e1', 'a' * 64, ('python',), '.', b'', 1, 100, {})
    with pytest.raises(ValueError, match='execution_capabilities_missing'):
        owner.start(request)
    assert calls == []
    owner.close()
    db.close()


def test_revocation_persists_across_owner_restart(tmp_path):
    path = tmp_path / 'owner.db'
    db = sqlite3.connect(path, isolation_level=None)
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    assert owner.revoke('auth', 3)['executions'] == []
    owner.close()
    db.close()
    db = sqlite3.connect(path, isolation_level=None)
    try:
        owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
        assert not owner._authorized('auth', 3)
        import time
        with pytest.raises(ValueError, match='execution_permit_authorization_revoked'):
            owner.authorize('auth', 3, time.time() + 30)
        owner.authorize('auth', 4, time.time() + 30)
        assert owner._authorized('auth', 4)
        owner.close()
    finally:
        db.close()


def test_declared_capabilities_remain_missing_on_new_instance():
    db = sqlite3.connect(':memory:', isolation_level=None)
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    owner.record_self_test(False, source='explicit_test_denied')
    assert owner.capabilities().missing()
    assert owner.capabilities().features['filesystem_isolation'].state == 'unsupported'
    owner.close()
    db.close()


def test_invalid_lease_never_updates_execution():
    db = sqlite3.connect(':memory:', isolation_level=None)
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    for value in (True, 0, 11, float('nan')):
        with pytest.raises(ValueError, match='execution_lease_invalid'):
            owner.renew('e1', value)
    owner.close()
    db.close()


def test_running_event_failure_closes_unreleased_gate(tmp_path, monkeypatch):
    import time
    from dataclasses import replace
    from test_execution_permit import fixture
    from butian.runtime.execution_permit import capability_digest, request_digest
    db = sqlite3.connect(':memory:', isolation_level=None)
    calls = []
    class Gate:
        def abort_unreleased(self):
            calls.append('abort_gate')
        def confirmed_exit(self):
            return True
        def close(self):
            calls.append('close')
    owner = LocalExecutionOwner(db, 'executor', authorization_active=lambda *_: True,
                               process_factory=lambda *args: Gate())
    owner.record_self_test(True, source='test_fixture_only')
    request, _, _ = fixture(tmp_path)
    permit = {**request.permit, 'instance_id': owner.instance_id,
              'capability_sha256': capability_digest(owner.capabilities()),
              'expires_at': time.time() + 30}
    request = replace(request, permit=permit)
    request = replace(request, request_sha256=request_digest(request))
    owner.authorize('auth', 1, time.time() + 30)
    original = owner._append
    def fail_running(execution_id, state, **kwargs):
        if state == 'running':
            raise sqlite3.OperationalError('injected write failure')
        return original(execution_id, state, **kwargs)
    monkeypatch.setattr(owner, '_append', fail_running)
    with pytest.raises(sqlite3.OperationalError):
        owner.start(request)
    assert calls == ['abort_gate', 'close']
    assert owner._active == {}
    assert owner.inspect('e1').state == 'unknown'
    owner.close()
    db.close()


def test_close_preserves_unconfirmed_job_without_worker():
    db = sqlite3.connect(':memory:', isolation_level=None)
    calls = []
    class Unconfirmed:
        def stop(self):
            calls.append('stop')
            raise PermissionError('denied')
        def confirmed_exit(self):
            return False
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    owner.ledger.accept('unstarted', 'a' * 64)
    owner._active['unstarted'] = {'process': Unconfirmed(), 'reason': None,
                                'stop_error': None}
    with pytest.raises(RuntimeError, match='shutdown_incomplete_active_tool_executions'):
        owner.close()
    assert calls == ['stop']
    assert 'unstarted' in owner._active
    db.close()


def test_worker_finalization_signals_waiters_when_exit_query_fails(monkeypatch):
    monkeypatch.setattr('butian.runtime.execution_owner.validate_permit', lambda *args: None)
    import threading
    from types import SimpleNamespace
    db = sqlite3.connect(':memory:', isolation_level=None)
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    owner.ledger.accept('query-failure', 'a' * 64)
    import io
    class BrokenQuery:
        process = SimpleNamespace(stdout=io.BytesIO(), stderr=io.BytesIO())
        def release(self):
            pass
        def confirmed_exit(self):
            raise OSError('query failed')
    active = {'process': BrokenQuery(), 'request': SimpleNamespace(output_limit=100),
              'reason': None, 'stop_error': None, 'finished': threading.Event()}
    owner._active['query-failure'] = active
    owner._run('query-failure', active)
    assert active['finished'].is_set()
    assert 'query-failure' in owner._active
    assert owner.inspect('query-failure').state == 'unknown'
    db.close()


def test_stop_still_controls_owned_job_when_audit_write_fails(monkeypatch):
    db = sqlite3.connect(':memory:', isolation_level=None)
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    calls = []
    class Owned:
        def stop(self):
            calls.append('stop')
    active = {'process': Owned(), 'reason': None, 'stop_error': None}
    def fail_append(*args, **kwargs):
        raise sqlite3.OperationalError('audit unavailable')
    monkeypatch.setattr(owner, '_append', fail_append)
    owner._stop('owned', active, 'timeout')
    assert calls == ['stop']
    assert active['audit_error'] == 'OperationalError'
    owner._stop('owned', active, 'timeout')
    assert calls == ['stop']
    db.close()


def test_worker_persistent_audit_failure_remains_queryable_unknown(monkeypatch):
    import io
    import threading
    from types import SimpleNamespace
    monkeypatch.setattr('butian.runtime.execution_owner.validate_permit', lambda *args: None)
    db = sqlite3.connect(':memory:', isolation_level=None)
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    owner.ledger.accept('audit-failure', 'a' * 64)
    class Broken:
        process = SimpleNamespace(stdout=io.BytesIO(), stderr=io.BytesIO())
        def release(self):
            pass
        def confirmed_exit(self):
            raise OSError('query failed')
    active = {'process': Broken(), 'request': SimpleNamespace(output_limit=100),
              'reason': None, 'stop_error': None, 'finished': threading.Event()}
    owner._active['audit-failure'] = active
    def fail_append(*args, **kwargs):
        raise sqlite3.OperationalError('persistent audit failure')
    monkeypatch.setattr(owner, '_append', fail_append)
    monkeypatch.setattr(owner.ledger, 'append', fail_append)
    owner._run('audit-failure', active)
    assert active['finished'].is_set()
    assert owner.inspect('audit-failure').state == 'unknown'
    assert owner.inspect('audit-failure').metadata['audit_persisted'] is False
    assert 'audit-failure' in owner._active
    db.close()


def test_release_gate_is_serialized_with_revocation(monkeypatch):
    import io
    import threading
    from types import SimpleNamespace
    monkeypatch.setattr('butian.runtime.execution_owner.validate_permit', lambda *args: None)
    db = sqlite3.connect(':memory:', isolation_level=None, check_same_thread=False)
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    owner.ledger.accept('gated', 'a' * 64)
    owner._append('gated', 'running')
    calls = []
    class Gate:
        process = SimpleNamespace(stdout=io.BytesIO(), stderr=io.BytesIO(), returncode=0)
        def release_gate(self):
            assert owner._lock._is_owned()
            calls.append('gate')
        def release_input(self):
            assert not owner._lock._is_owned()
            calls.append('input')
        def confirmed_exit(self):
            return True
        def close(self):
            calls.append('close')
    active = {'process': Gate(), 'request': SimpleNamespace(output_limit=100),
              'stdout': bytearray(), 'stderr': bytearray(), 'reason': None,
              'stop_error': None, 'finished': threading.Event()}
    owner._active['gated'] = active
    owner._run('gated', active)
    assert calls == ['gate', 'input', 'close']
    assert owner.inspect('gated').state == 'success'
    db.close()


def test_launch_and_unknown_audit_failure_retains_queryable_evidence(tmp_path, monkeypatch):
    import time
    from dataclasses import replace
    from test_execution_permit import fixture
    from butian.runtime.execution_permit import capability_digest, request_digest
    db = sqlite3.connect(':memory:', isolation_level=None)
    class Gate:
        def abort_unreleased(self): pass
        def confirmed_exit(self): return True
        def close(self): pass
    owner = LocalExecutionOwner(db, 'executor', authorization_active=lambda *_: True,
                                process_factory=lambda *args: Gate())
    owner.record_self_test(True, source='test_fixture_only')
    request, _, _ = fixture(tmp_path)
    permit = {**request.permit, 'instance_id': owner.instance_id,
              'capability_sha256': capability_digest(owner.capabilities()), 'expires_at': time.time() + 30}
    request = replace(request, permit=permit)
    request = replace(request, request_sha256=request_digest(request))
    owner.authorize('auth', 1, time.time() + 30)
    original = owner._append
    def fail_running(execution_id, state, **kwargs):
        if state == 'running':
            raise sqlite3.OperationalError('audit unavailable')
        return original(execution_id, state, **kwargs)
    monkeypatch.setattr(owner, '_append', fail_running)
    monkeypatch.setattr(owner.ledger, 'append', lambda observation: (_ for _ in ()).throw(sqlite3.OperationalError('audit unavailable')))
    try:
        with pytest.raises(sqlite3.OperationalError):
            owner.start(request)
        observation = owner.inspect('e1')
        assert observation.state == 'unknown'
        assert observation.metadata['audit_persisted'] is False
        with pytest.raises(RuntimeError, match='shutdown_incomplete'):
            owner.close()
    finally:
        db.close()


def test_unreleased_gate_timeout_preserves_unknown_and_owner(tmp_path, monkeypatch):
    import subprocess
    import time
    from dataclasses import replace
    from test_execution_permit import fixture
    from butian.runtime.execution_permit import capability_digest, request_digest
    db = sqlite3.connect(':memory:', isolation_level=None)
    calls = []
    class Gate:
        def abort_unreleased(self):
            raise subprocess.TimeoutExpired('trusted-bootstrap', 5)
        def confirmed_exit(self): return False
        def close(self): calls.append('close')
        def stop(self):
            calls.append('stop')
            raise PermissionError('injected denial')
    owner = LocalExecutionOwner(db, 'executor', authorization_active=lambda *_: True,
                                process_factory=lambda *args: Gate())
    owner.record_self_test(True, source='test_fixture_only')
    request, _, _ = fixture(tmp_path)
    request = replace(request, permit={**request.permit, 'instance_id': owner.instance_id,
        'capability_sha256': capability_digest(owner.capabilities()), 'expires_at': time.time() + 30})
    request = replace(request, request_sha256=request_digest(request))
    owner.authorize('auth', 1, time.time() + 30)
    original = owner._append
    def fail_running(execution_id, state, **kwargs):
        if state == 'running':
            raise sqlite3.OperationalError('injected launch audit failure')
        return original(execution_id, state, **kwargs)
    monkeypatch.setattr(owner, '_append', fail_running)
    try:
        with pytest.raises(sqlite3.OperationalError, match='injected launch audit failure'):
            owner.start(request)
        assert owner.inspect('e1').state == 'unknown'
        assert not owner.inspect('e1').tree_exit_confirmed
        assert 'e1' in owner._active
        assert calls == []
        with pytest.raises(RuntimeError, match='shutdown_incomplete'):
            owner.close()
        assert calls == ['stop']
    finally:
        db.close()


def test_recovered_audit_is_persisted_without_losing_live_owner(monkeypatch):
    from butian.runtime.execution_contract import ExecutionObservation
    db = sqlite3.connect(':memory:', isolation_level=None)
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    owner.ledger.accept('recover-audit', 'a' * 64)
    pending = ExecutionObservation('recover-audit', owner.executor_id, owner.instance_id,
        1, 'unknown', reason='audit_persistence_failure', metadata={'audit_persisted': False})
    class Process:
        def confirmed_exit(self): return False
    active = {'process': Process(), 'reason': 'launch_unconfirmed', 'stop_error': None,
              'worker': None, 'unpersisted_observation': pending}
    owner._active['recover-audit'] = active
    try:
        original_append = owner.ledger.append
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError('audit unavailable')
        monkeypatch.setattr(owner.ledger, 'append', unavailable)
        assert owner.inspect('recover-audit').metadata['audit_persisted'] is False
        assert len(owner.events('recover-audit', -1)) == 1
        assert 'recover-audit' in owner._active
        monkeypatch.setattr(owner.ledger, 'append', original_append)
        observation = owner.inspect('recover-audit')
        assert observation.state == 'unknown'
        assert observation.metadata['audit_persisted'] is True
        assert owner.ledger.inspect('recover-audit') == observation
        assert owner.events('recover-audit')[-1] == observation
        assert 'recover-audit' in owner._active
        with pytest.raises(RuntimeError, match='shutdown_incomplete'):
            owner.close()
    finally:
        db.close()

@pytest.mark.parametrize('has_process', [False, True])
def test_audit_recovery_releases_only_confirmed_inactive_owner(has_process):
    from butian.runtime.execution_contract import ExecutionObservation
    db = sqlite3.connect(':memory:', isolation_level=None)
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    owner.ledger.accept('finished-audit', 'a' * 64)
    calls = []
    class Exited:
        def confirmed_exit(self): return True
        def close(self): calls.append('close')
    pending = ExecutionObservation('finished-audit', owner.executor_id, owner.instance_id,
        1, 'unknown', reason='audit_persistence_failure', metadata={'audit_persisted': False})
    owner._active['finished-audit'] = {'process': Exited() if has_process else None,
        'reason': 'launch_unconfirmed', 'stop_error': None, 'worker': None,
        'unpersisted_observation': pending}
    try:
        result = owner.inspect('finished-audit')
        assert result.state == 'unknown'
        assert result.tree_exit_confirmed is has_process
        assert result.metadata['audit_persisted'] is True
        assert 'finished-audit' not in owner._active
        assert calls == (['close'] if has_process else [])
        owner.close()
    finally:
        db.close()

def test_recovered_unknown_owner_can_release_after_later_confirmed_exit():
    from butian.runtime.execution_contract import ExecutionObservation
    db = sqlite3.connect(':memory:', isolation_level=None)
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    owner.ledger.accept('later-exit', 'a' * 64)
    class Process:
        exited = False
        closed = False
        def confirmed_exit(self): return self.exited
        def close(self): self.closed = True
    process = Process()
    pending = ExecutionObservation('later-exit', owner.executor_id, owner.instance_id,
        1, 'unknown', metadata={'audit_persisted': False})
    owner._active['later-exit'] = {'process': process, 'reason': 'launch_unconfirmed',
        'stop_error': None, 'worker': None, 'unpersisted_observation': pending}
    try:
        assert owner.inspect('later-exit').metadata['audit_persisted'] is True
        assert 'later-exit' in owner._active
        process.exited = True
        result = owner.inspect('later-exit')
        assert result.state == 'unknown'
        assert result.tree_exit_confirmed is True
        assert result.metadata['exit_confirmation_source'] == 'owned_job_query'
        assert process.closed
        assert 'later-exit' not in owner._active
        owner.close()
    finally:
        db.close()

def test_close_recovers_audit_and_releases_confirmed_exit_without_stop():
    from butian.runtime.execution_contract import ExecutionObservation
    db = sqlite3.connect(':memory:', isolation_level=None)
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    owner.ledger.accept('close-recovery', 'a' * 64)
    calls = []
    class Exited:
        def confirmed_exit(self): return True
        def close(self): calls.append('close')
        def stop(self): pytest.fail('confirmed exit must not invoke stop')
    owner._active['close-recovery'] = {'process': Exited(), 'reason': None,
        'stop_error': None, 'worker': None,
        'unpersisted_observation': ExecutionObservation('close-recovery', owner.executor_id,
            owner.instance_id, 1, 'unknown', metadata={'audit_persisted': False})}
    try:
        owner.close()
        assert calls == ['close']
        assert owner.ledger.inspect('close-recovery').metadata['audit_persisted'] is True
    finally:
        db.close()

def test_confirmed_exit_audit_failure_retains_owner(monkeypatch):
    from butian.runtime.execution_contract import ExecutionObservation
    db = sqlite3.connect(':memory:', isolation_level=None)
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    owner.ledger.accept('exit-audit', 'a' * 64)
    owner.ledger.append(ExecutionObservation('exit-audit', owner.executor_id,
        owner.instance_id, 1, 'unknown', reason='stop_unconfirmed'))
    calls = []
    class Exited:
        def confirmed_exit(self): return True
        def close(self): calls.append('close')
    owner._active['exit-audit'] = {'process': Exited(), 'worker': None}
    append = owner.ledger.append
    def fail(*args): raise sqlite3.OperationalError('audit unavailable')
    monkeypatch.setattr(owner.ledger, 'append', fail)
    try:
        assert owner.inspect('exit-audit').tree_exit_confirmed is False
        assert 'exit-audit' in owner._active
        assert calls == []
        monkeypatch.setattr(owner.ledger, 'append', append)
        assert owner.inspect('exit-audit').tree_exit_confirmed is True
        assert calls == ['close']
        assert 'exit-audit' not in owner._active
    finally:
        db.close()

def test_worker_unknown_exit_persists_confirmation_before_release(monkeypatch):
    import io
    import threading
    from types import SimpleNamespace
    from butian.runtime.execution_contract import ExecutionObservation
    monkeypatch.setattr('butian.runtime.execution_owner.validate_permit', lambda *args: None)
    db = sqlite3.connect(':memory:', isolation_level=None)
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    owner.ledger.accept('worker-exit', 'a' * 64)
    owner.ledger.append(ExecutionObservation('worker-exit', owner.executor_id,
        owner.instance_id, 1, 'unknown', reason='stop_unconfirmed'))
    class Exited:
        process = SimpleNamespace(stdout=io.BytesIO(), stderr=io.BytesIO())
        def release(self): pass
        def confirmed_exit(self): return True
        def close(self): pass
    active = {'process': Exited(), 'request': SimpleNamespace(output_limit=100),
        'reason': None, 'stop_error': None, 'finished': threading.Event()}
    owner._active['worker-exit'] = active
    try:
        owner._run('worker-exit', active)
        result = owner.ledger.inspect('worker-exit')
        assert result.state == 'unknown'
        assert result.tree_exit_confirmed is True
        assert 'worker-exit' not in owner._active
    finally:
        db.close()

def test_gate_preparation_does_not_hold_revocation_lock(monkeypatch):
    import io
    import threading
    from types import SimpleNamespace
    monkeypatch.setattr('butian.runtime.execution_owner.validate_permit', lambda *args: None)
    db = sqlite3.connect(':memory:', isolation_level=None, check_same_thread=False)
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True)
    owner.ledger.accept('prepared-gate', 'a' * 64)
    owner._append('prepared-gate', 'running')
    calls = []
    class Gate:
        process = SimpleNamespace(stdout=io.BytesIO(), stderr=io.BytesIO(), returncode=0)
        def prepare_gate(self):
            assert not owner._lock._is_owned()
            calls.append('prepare')
        def release_gate(self):
            assert owner._lock._is_owned()
            assert calls == ['prepare']
            calls.append('release')
        def release_input(self): pass
        def confirmed_exit(self): return True
        def close(self): calls.append('close')
    active = {'process': Gate(), 'request': SimpleNamespace(output_limit=100),
        'stdout': bytearray(), 'stderr': bytearray(), 'reason': None,
        'stop_error': None, 'finished': threading.Event()}
    owner._active['prepared-gate'] = active
    try:
        owner._run('prepared-gate', active)
        assert calls == ['prepare', 'release', 'close']
        assert owner.inspect('prepared-gate').state == 'success'
    finally:
        db.close()


def test_constructor_failure_transfers_unconfirmed_owner_without_retry(monkeypatch):
    monkeypatch.setattr('butian.runtime.execution_owner.validate_permit', lambda *args: None)
    db = sqlite3.connect(':memory:', isolation_level=None)
    calls = []
    class Bootstrap:
        exited = False
        def abort_unreleased(self): pytest.fail('constructor already attempted EOF')
        def stop(self): pytest.fail('inspection must not substitute termination')
        def confirmed_exit(self): return self.exited
        def close(self): calls.append('close')
    bootstrap = Bootstrap()
    error = PermissionError('assignment denied')
    error.owned_process = bootstrap
    error.gate_cleanup_error = 'TimeoutExpired'
    def construct(*args): raise error
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True,
                                process_factory=construct)
    request = ExecutionRequest('transfer', 'a' * 64, ('python',), '.', b'', 1, 100, {})
    try:
        with pytest.raises(PermissionError) as caught:
            owner.start(request)
        assert caught.value is error
        assert owner._active['transfer']['process'] is bootstrap
        assert owner._active['transfer']['gate_cleanup_error'] == 'TimeoutExpired'
        result = owner.inspect('transfer')
        assert result.state == 'unknown'
        assert result.reason == 'launch_unconfirmed'
        assert result.tree_exit_confirmed is False
        assert calls == []
        bootstrap.exited = True
        result = owner.inspect('transfer')
        assert result.state == 'unknown'
        assert result.tree_exit_confirmed is True
        assert calls == ['close']
        assert 'transfer' not in owner._active
        owner.close()
    finally:
        db.close()


def test_owner_restart_marks_previous_run_unknown_without_process_calls():
    from butian.runtime.execution_ledger import ExecutionLedger
    from butian.runtime.execution_contract import ExecutionObservation
    db = sqlite3.connect(':memory:', isolation_level=None)
    old = ExecutionLedger(db, 'owner', 'old-instance')
    old.accept('lost', 'a' * 64)
    old.append(ExecutionObservation('lost', 'owner', 'old-instance', 1, 'running'))
    calls = []
    owner = LocalExecutionOwner(db, 'owner', authorization_active=lambda *_: True,
                                process_factory=lambda *args: calls.append(args))
    value = owner.inspect('lost')
    assert value.state == 'unknown'
    assert value.instance_id == 'old-instance'
    assert value.tree_exit_confirmed is False
    assert calls == []
    assert owner._active == {}
    owner.close()
    db.close()
