import pytest

from test_tools import MANIFEST, make_service, propose, registered_tool_proofs


@pytest.mark.parametrize('audit_fails', [False, True])
def test_verification_preserves_outer_transaction(tmp_path, monkeypatch, audit_fails):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, 'print(1)', MANIFEST)
        proofs = registered_tool_proofs(service, 'm1', ['outer-verification'])
        db = service.store.db
        original = dict(db.execute('SELECT * FROM tool_assets WHERE tool_id=?', (tool_id,)).fetchone())
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE tool_assets SET updated_at=updated_at+1 WHERE tool_id=?', (tool_id,))
        caller_state = dict(db.execute('SELECT * FROM tool_assets WHERE tool_id=?', (tool_id,)).fetchone())
        if audit_fails:
            def fail(*args, **kwargs):
                raise RuntimeError('verification_audit_fault')
            monkeypatch.setattr(service.tools, '_event', fail)
            with pytest.raises(RuntimeError, match='verification_audit_fault'):
                service.verify_tool('m1', tool_id, 1, 'unknown', independent_key='outer', evidence_ids=proofs)
            assert dict(db.execute('SELECT * FROM tool_assets WHERE tool_id=?', (tool_id,)).fetchone()) == caller_state
            assert db.execute('SELECT COUNT(*) FROM tool_verifications WHERE tool_id=?', (tool_id,)).fetchone()[0] == 0
        else:
            service.verify_tool('m1', tool_id, 1, 'unknown', independent_key='outer', evidence_ids=proofs)
        assert db.in_transaction
        db.rollback()
        assert dict(db.execute('SELECT * FROM tool_assets WHERE tool_id=?', (tool_id,)).fetchone()) == original
        assert db.execute('SELECT COUNT(*) FROM tool_verifications WHERE tool_id=?', (tool_id,)).fetchone()[0] == 0
    finally:
        service.close()


def test_archive_audit_failure_rolls_back_state(tmp_path):
    import sqlite3
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        db = service.store.db
        original = dict(db.execute('SELECT * FROM tool_assets WHERE tool_id=?', (tool_id,)).fetchone())
        db.execute("CREATE TEMP TRIGGER archive_audit_fault BEFORE INSERT ON tool_events WHEN NEW.event_type='status_changed' BEGIN SELECT RAISE(ABORT,'archive_audit_fault'); END")
        with pytest.raises(sqlite3.IntegrityError, match='archive_audit_fault'):
            service.archive_tool('m1', tool_id)
        assert dict(db.execute('SELECT * FROM tool_assets WHERE tool_id=?', (tool_id,)).fetchone()) == original
        db.execute('BEGIN')
        with pytest.raises(sqlite3.IntegrityError, match='archive_audit_fault'):
            service.archive_tool('m1', tool_id)
        assert db.in_transaction
        assert dict(db.execute('SELECT * FROM tool_assets WHERE tool_id=?', (tool_id,)).fetchone()) == original
        db.rollback()
    finally:
        service.close()
