import pytest

from butian.runtime import execution_self_test


def test_self_test_preserves_fixed_failure_reason(tmp_path, monkeypatch):
    class Owner:
        executor_id = 'executor'
        instance_id = 'instance'
        def record_self_test(self, confirmed, *, source):
            assert confirmed is False
    def unavailable(*args):
        raise RuntimeError('executor_self_test_readiness_unconfirmed')
    monkeypatch.setattr(execution_self_test, 'OwnedProcess', unavailable)
    result = execution_self_test.verify_process_control(Owner(), tmp_path)
    assert result['verified'] is False
    assert result['reason'] == 'executor_self_test_readiness_unconfirmed'
