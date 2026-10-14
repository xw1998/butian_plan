import sqlite3

import pytest

from butian.runtime.execution_protocol import MessageVerifier, sign_message

KEY = b'x' * 32


def verifier(db=None, instance='instance-1'):
    return MessageVerifier(db or sqlite3.connect(':memory:', isolation_level=None), KEY,
                           'executor-1', instance)


def message():
    return sign_message(KEY, 'executor-1', 'instance-1', 'inspect', {'execution_id': 'e1'}, now=100)


def test_authenticated_message_and_persistent_replay_rejection(tmp_path):
    path = tmp_path / 'executor.db'
    db = sqlite3.connect(path, isolation_level=None)
    request = message()
    assert verifier(db).verify(request, now=100)['payload']['execution_id'] == 'e1'
    db.close()
    db = sqlite3.connect(path, isolation_level=None)
    try:
        with pytest.raises(ValueError, match='executor_message_replayed'):
            verifier(db).verify(request, now=100)
    finally:
        db.close()


def test_payload_tampering_rejected_before_replay_insert():
    request = message()
    original = dict(request['payload'])
    request['payload']['execution_id'] = 'other'
    check = verifier()
    with pytest.raises(ValueError, match='executor_authentication_failed'):
        check.verify(request, now=100)
    request['payload'] = original
    check.verify(request, now=100)


def test_old_instance_and_expired_message_rejected():
    with pytest.raises(ValueError, match='executor_identity_mismatch'):
        verifier(instance='instance-2').verify(message(), now=100)
    with pytest.raises(ValueError, match='executor_message_expired'):
        verifier().verify(message(), now=131)


def test_unsupported_protocol_and_action_rejected():
    request = sign_message(KEY, 'executor-1', 'instance-1', 'arbitrary-command', {}, now=100)
    with pytest.raises(ValueError, match='executor_action_invalid'):
        verifier().verify(request, now=100)


def test_failed_persistence_does_not_authorize_message():
    db = sqlite3.connect(':memory:', isolation_level=None)
    check = verifier(db)
    db.execute("CREATE TRIGGER reject_nonce BEFORE INSERT ON executor_nonces BEGIN SELECT RAISE(ABORT,'storage failure'); END")
    with pytest.raises(ValueError):
        check.verify(message(), now=100)
