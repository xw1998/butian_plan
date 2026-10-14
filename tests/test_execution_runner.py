import sqlite3
from types import SimpleNamespace

import pytest

from butian.runtime.execution_dispatch import ExecutionDispatch
from butian.runtime.execution_runner import ExecutionRunner
from test_execution_permit import fixture


def test_cancel_before_dispatch_is_durable_and_never_starts(tmp_path):
    request, caps, _ = fixture(tmp_path)
    calls = []
    adapter = SimpleNamespace(capabilities=lambda: caps,
        authorize=lambda *args: calls.append('authorize'),
        start=lambda *args: calls.append('start'),
        cancel=lambda *args, **kwargs: calls.append('cancel'))
    path = tmp_path / 'dispatch.db'
    db = sqlite3.connect(path, isolation_level=None)
    runner = ExecutionRunner(ExecutionDispatch(db, adapter))
    result = runner.cancel(request.execution_id, reason='actor_cancel')
    assert result.state == 'cancelling'
    assert not result.tree_exit_confirmed
    db.close()
    db = sqlite3.connect(path, isolation_level=None)
    runner = ExecutionRunner(ExecutionDispatch(db, adapter))
    with pytest.raises(ValueError, match='execution_cancelled_before_dispatch'):
        runner.run(request, authorization_valid=lambda: True)
    assert calls == []
    db.close()


def test_direct_dispatch_also_rejects_durable_cancellation(tmp_path):
    request, caps, _ = fixture(tmp_path)
    calls = []
    adapter = SimpleNamespace(capabilities=lambda: caps,
        start=lambda *args: calls.append('start'),
        cancel=lambda *args, **kwargs: calls.append('cancel'))
    db = sqlite3.connect(':memory:', isolation_level=None)
    dispatch = ExecutionDispatch(db, adapter)
    dispatch.cancel(request.execution_id, reason='actor_cancel')
    with pytest.raises(ValueError, match='execution_cancelled_before_dispatch'):
        dispatch.start(request)
    assert calls == []
    assert db.execute('SELECT count(*) FROM execution_dispatch').fetchone()[0] == 0
    db.close()


def test_active_without_dispatch_record_remains_shutdown_blocker():
    adapter = SimpleNamespace()
    db = sqlite3.connect(':memory:', isolation_level=None)
    runner = ExecutionRunner(ExecutionDispatch(db, adapter))
    runner._active.add('unconfirmed-authorization')
    assert runner.unresolved() == ['unconfirmed-authorization']
    assert runner.wait(timeout=0) is False
    db.close()

def test_confirmed_unknown_exit_releases_runner_shutdown_blocker():
    from butian.runtime.execution_contract import ExecutionObservation
    from test_execution_dispatch import Adapter, request
    db = sqlite3.connect(':memory:', isolation_level=None)
    adapter = Adapter()
    dispatch = ExecutionDispatch(db, adapter)
    dispatch.start(request())
    unconfirmed = ExecutionObservation('e1', 'owner', 'i1', 2, 'unknown', reason='stop_unconfirmed')
    dispatch.record(unconfirmed)
    confirmed = ExecutionObservation('e1', 'owner', 'i1', 3, 'unknown', reason='stop_unconfirmed', tree_exit_confirmed=True)
    adapter.inspect = lambda _: confirmed
    runner = ExecutionRunner(dispatch)
    runner._active.add('e1')
    try:
        assert runner.wait(timeout=0) is True
        assert runner.unresolved() == []
        assert db.execute('SELECT last_state FROM execution_dispatch').fetchone()[0] == 'unknown'
    finally:
        db.close()

@pytest.mark.parametrize('payload', [None, '{}', 'null', '{"execution_id":"other","state":"unknown","tree_exit_confirmed":true}', '{"execution_id":"e1","state":"unknown","tree_exit_confirmed":1}'])
def test_unknown_without_valid_persisted_exit_remains_blocker(payload):
    from test_execution_dispatch import Adapter, request
    db = sqlite3.connect(':memory:', isolation_level=None)
    dispatch = ExecutionDispatch(db, Adapter())
    dispatch.start(request())
    db.execute("UPDATE execution_dispatch SET last_state='unknown',last_observation=?", (payload,))
    runner = ExecutionRunner(dispatch)
    try:
        assert runner.unresolved() == ['e1']
    finally:
        db.close()
