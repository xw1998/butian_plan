import hashlib
import json
import pytest
from test_tools import make_service, propose, MANIFEST


def fixture_record(service):
    tool_id = propose(service)['tool_id']
    service.add_tool_version('m1', tool_id, "print('done')", MANIFEST)
    record = dict(execution_id='exec_replay', tool_id=tool_id, version=1,
                  actor_member_id='m1', task_id=None, status='unknown',
                  started_at=1.0, finished_at=2.0, termination_reason='process_exit_unconfirmed',
                  audit_status='persistence_failed', error_type='OperationalError')
    path = service.home / 'execution_recovery' / 'exec_replay.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(record), encoding='utf-8')
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_replay_preserves_evidence_and_is_idempotent(tmp_path):
    service = make_service(tmp_path)
    try:
        path, digest = fixture_record(service)
        result = service.tools.replay_execution_audit('m1', 'exec_replay', expected_sha256=digest)
        assert result['audit_status'] == 'recovered'
        assert result['status'] == 'unknown'
        assert path.exists()
        again = service.tools.replay_execution_audit('m1', 'exec_replay', expected_sha256=digest)
        assert again['idempotent'] is True
        assert service.store.db.execute("SELECT COUNT(*) FROM tool_events WHERE event_type='execution_audit_recovered'").fetchone()[0] == 1
    finally:
        service.close()


def test_replay_rejects_wrong_actor_and_changed_evidence(tmp_path):
    service = make_service(tmp_path)
    try:
        path, digest = fixture_record(service)
        with pytest.raises(ValueError, match='actor'):
            service.tools.replay_execution_audit('m2', 'exec_replay', expected_sha256=digest)
        path.write_text(path.read_text() + ' ', encoding='utf-8')
        with pytest.raises(ValueError, match='hash'):
            service.tools.replay_execution_audit('m1', 'exec_replay', expected_sha256=digest)
        assert service.store.db.execute("SELECT COUNT(*) FROM tool_executions").fetchone()[0] == 0
    finally:
        service.close()


@pytest.mark.parametrize('field,value', [('version', True), ('status', 'success'), ('started_at', float('nan')), ('finished_at', 0)])
def test_replay_rejects_invalid_metadata(tmp_path, field, value):
    service = make_service(tmp_path)
    try:
        path, _ = fixture_record(service)
        record = json.loads(path.read_text())
        record[field] = value
        path.write_text(json.dumps(record), encoding='utf-8')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with pytest.raises(ValueError):
            service.tools.replay_execution_audit('m1', 'exec_replay', expected_sha256=digest)
        assert service.store.db.execute('SELECT COUNT(*) FROM tool_executions').fetchone()[0] == 0
    finally:
        service.close()


def test_replay_event_failure_rolls_back_and_preserves_file(tmp_path, monkeypatch):
    import sqlite3
    service = make_service(tmp_path)
    try:
        path, digest = fixture_record(service)
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError('injected audit failure')
        monkeypatch.setattr(service.tools, '_event', fail)
        with pytest.raises(sqlite3.OperationalError):
            service.tools.replay_execution_audit('m1', 'exec_replay', expected_sha256=digest)
        assert path.exists()
        assert service.store.db.execute('SELECT COUNT(*) FROM tool_executions').fetchone()[0] == 0
    finally:
        service.close()


def test_replay_changed_record_after_commit_conflicts(tmp_path):
    service = make_service(tmp_path)
    try:
        path, digest = fixture_record(service)
        service.tools.replay_execution_audit('m1', 'exec_replay', expected_sha256=digest)
        record = json.loads(path.read_text())
        record['status'] = 'failed'
        path.write_text(json.dumps(record), encoding='utf-8')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with pytest.raises(ValueError, match='conflict'):
            service.tools.replay_execution_audit('m1', 'exec_replay', expected_sha256=digest)
        assert service.store.db.execute('SELECT status FROM tool_executions').fetchone()[0] == 'unknown'
    finally:
        service.close()
