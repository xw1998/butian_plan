import sqlite3

import pytest

from butian.runtime.execution_contract import ExecutionObservation
from butian.runtime.execution_ledger import ExecutionLedger


def ledger(db=None, instance='i1'):
    return ExecutionLedger(db or sqlite3.connect(':memory:', isolation_level=None), 'owner', instance)


def observation(sequence, state='running', **kwargs):
    return ExecutionObservation('e1', 'owner', 'i1', sequence, state, **kwargs)


def test_repeated_intent_does_not_allow_second_spawn():
    store = ledger()
    assert store.accept('e1', 'a' * 64)[1]
    assert not store.accept('e1', 'a' * 64)[1]
    with pytest.raises(ValueError, match='execution_id_request_conflict'):
        store.accept('e1', 'b' * 64)


def test_reconnect_returns_ordered_events_without_replaying(tmp_path):
    path = tmp_path / 'ledger.db'
    db = sqlite3.connect(path, isolation_level=None)
    store = ledger(db)
    store.accept('e1', 'a' * 64)
    store.append(observation(1))
    store.append(observation(2, 'cancelling', reason='authorization_revoked'))
    db.close()
    db = sqlite3.connect(path, isolation_level=None)
    try:
        store = ledger(db, 'i2')
        assert not store.accept('e1', 'a' * 64)[1]
        assert [item.sequence for item in store.events('e1', 0)] == [1, 2]
        assert store.previous_unconfirmed()[0].state == 'cancelling'
    finally:
        db.close()


def test_gap_and_wrong_generation_cannot_modify_ledger():
    store = ledger()
    store.accept('e1', 'a' * 64)
    with pytest.raises(ValueError, match='execution_event_sequence_gap'):
        store.append(observation(2))
    with pytest.raises(ValueError, match='execution_event_identity_mismatch'):
        store.append(ExecutionObservation('e1', 'owner', 'other', 1, 'running'))
    assert store.inspect('e1').state == 'accepted'


def test_event_persistence_failure_keeps_previous_state():
    db = sqlite3.connect(':memory:', isolation_level=None)
    store = ledger(db)
    store.accept('e1', 'a' * 64)
    db.execute("CREATE TRIGGER reject_event BEFORE INSERT ON executor_events BEGIN SELECT RAISE(ABORT,'storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.append(observation(1))
    assert store.inspect('e1').sequence == 0
    assert len(store.events('e1')) == 1


def test_unknown_cannot_be_relabelled_success_without_evidence():
    store = ledger()
    store.accept('e1', 'a' * 64)
    store.append(observation(1, 'unknown', reason='owner_unreachable'))
    with pytest.raises(ValueError, match='execution_state_transition_invalid'):
        store.append(observation(2, 'success', exit_code=0, tree_exit_confirmed=True))


def test_two_connections_only_one_acceptance(tmp_path):
    path = tmp_path / 'ledger.db'
    first_db = sqlite3.connect(path, isolation_level=None)
    second_db = sqlite3.connect(path, isolation_level=None)
    try:
        first, second = ledger(first_db), ledger(second_db)
        assert first.accept('e1', 'a' * 64)[1]
        assert not second.accept('e1', 'a' * 64)[1]
    finally:
        first_db.close()
        second_db.close()

def test_unknown_can_append_confirmed_exit_without_relabelling_result():
    store = ledger()
    store.accept('e1', 'a' * 64)
    store.append(observation(1, 'unknown', reason='stop_unconfirmed'))
    store.append(observation(2, 'unknown', reason='stop_unconfirmed', tree_exit_confirmed=True,
        metadata={'exit_confirmation_source': 'owned_job_query'}))
    assert store.inspect('e1').state == 'unknown'
    assert store.inspect('e1').tree_exit_confirmed
    with pytest.raises(ValueError, match='execution_state_transition_invalid'):
        store.append(observation(3, 'unknown', reason='stop_unconfirmed'))


def test_explicit_restart_recovery_preserves_old_identity_and_never_replays():
    db = sqlite3.connect(':memory:', isolation_level=None)
    old = ledger(db)
    old.accept('e1', 'a' * 64)
    old.append(observation(1))
    restarted = ledger(db, 'i2')
    recovered = restarted.recover_previous_instances()
    assert len(recovered) == 1
    value = recovered[0]
    assert (value.executor_id, value.instance_id) == ('owner', 'i1')
    assert value.state == 'unknown'
    assert value.reason == 'executor_instance_lost'
    assert value.sequence == 2
    assert value.tree_exit_confirmed is False
    assert restarted.recover_previous_instances() == []
    assert not restarted.accept('e1', 'a' * 64)[1]
    assert [event.sequence for event in restarted.events('e1')] == [0, 1, 2]


def test_restart_recovery_is_atomic_on_persistence_failure():
    db = sqlite3.connect(':memory:', isolation_level=None)
    old = ledger(db)
    old.accept('e1', 'a' * 64)
    restarted = ledger(db, 'i2')
    db.execute("CREATE TRIGGER reject_recovery BEFORE INSERT ON executor_events BEGIN SELECT RAISE(ABORT,'storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        restarted.recover_previous_instances()
    assert restarted.inspect('e1').state == 'accepted'
    assert restarted.inspect('e1').sequence == 0
