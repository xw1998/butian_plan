import pytest

from test_tools import MANIFEST, make_service, propose


@pytest.mark.parametrize('operation', ['triage', 'request', 'provide'])
@pytest.mark.parametrize('audit_fails', [False, True])
def test_issue_information_preserves_caller_transaction(tmp_path, monkeypatch, operation, audit_fails):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, 'print(1)', MANIFEST)
        issue = service.report_tool_issue('m1', tool_id, 1, description='Observed incorrect output')
        if operation == 'provide':
            service.tools.request_issue_information('m1', issue['issue_id'], request='Provide reproduction')
        original = dict(service.store.db.execute('SELECT * FROM tool_issues WHERE issue_id=?', (issue['issue_id'],)).fetchone())
        service.store.db.execute('BEGIN IMMEDIATE')
        service.store.db.execute("UPDATE tool_issues SET triage_reason='caller pending' WHERE issue_id=?", (issue['issue_id'],))
        if audit_fails:
            def fail(*args, **kwargs):
                raise RuntimeError('information_audit_fault')
            monkeypatch.setattr(service.tools, '_event', fail)
        actions = {
            'triage': lambda: service.tools.triage_issue('m1', issue['issue_id']),
            'request': lambda: service.tools.request_issue_information('m1', issue['issue_id'], request='Provide reproduction'),
            'provide': lambda: service.tools.provide_issue_information('m1', issue['issue_id'], information='Reproduction details'),
        }
        if audit_fails:
            with pytest.raises(RuntimeError, match='information_audit_fault'):
                actions[operation]()
            current = dict(service.store.db.execute('SELECT * FROM tool_issues WHERE issue_id=?', (issue['issue_id'],)).fetchone())
            assert current == {**original, 'triage_reason': 'caller pending'}
        else:
            result = actions[operation]()
            assert result['status'] == {'triage': 'triaged', 'request': 'needs_information', 'provide': 'open'}[operation]
        assert service.store.db.in_transaction
        service.store.db.rollback()
        restored = dict(service.store.db.execute('SELECT * FROM tool_issues WHERE issue_id=?', (issue['issue_id'],)).fetchone())
        assert restored == original
    finally:
        service.close()


@pytest.mark.parametrize('outer_transaction', [False, True])
def test_duplicate_link_audit_failure_is_atomic(tmp_path, monkeypatch, outer_transaction):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, 'print(1)', MANIFEST)
        parent = service.report_tool_issue('m1', tool_id, 1, description='Original issue')
        child = service.report_tool_issue('m1', tool_id, 1, description='Duplicate issue')
        db = service.store.db
        original = dict(db.execute('SELECT * FROM tool_issues WHERE issue_id=?', (child['issue_id'],)).fetchone())
        if outer_transaction:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE tool_issues SET triage_reason='caller pending' WHERE issue_id=?", (child['issue_id'],))
        def fail(*args, **kwargs):
            raise RuntimeError('duplicate_audit_fault')
        monkeypatch.setattr(service.tools, '_event', fail)
        with pytest.raises(RuntimeError, match='duplicate_audit_fault'):
            service.link_duplicate_tool_issue('m1', child['issue_id'], parent['issue_id'])
        current = dict(db.execute('SELECT * FROM tool_issues WHERE issue_id=?', (child['issue_id'],)).fetchone())
        assert current == {**original, **({'triage_reason': 'caller pending'} if outer_transaction else {})}
        assert db.in_transaction == outer_transaction
        if outer_transaction:
            db.rollback()
    finally:
        service.close()


@pytest.mark.parametrize('operation', ['escalate', 'claim', 'resolve'])
@pytest.mark.parametrize('audit_fails', [False, True])
def test_escalation_preserves_caller_transaction(tmp_path, monkeypatch, operation, audit_fails):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, 'print(1)', MANIFEST)
        issue = service.report_tool_issue('m1', tool_id, 1, description='Escalation issue')
        service.store.set_setting('dispatcher_member_id', 'm2')
        work_item = None
        if operation != 'escalate':
            work_item = service.tools.escalate_issue('m1', issue['issue_id'])['work_item_id']
        if operation == 'resolve':
            service.tools.claim_issue_escalation('m2', work_item)
        db = service.store.db
        def snapshot():
            return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')] for table in ('tool_issues', 'tool_issue_escalations', 'tool_issue_escalation_events', 'tool_events')}
        before = snapshot()
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE tool_issues SET triage_reason='caller pending' WHERE issue_id=?", (issue['issue_id'],))
        caller_state = snapshot()
        if audit_fails:
            db.execute("CREATE TEMP TRIGGER reject_escalation_event BEFORE INSERT ON tool_issue_escalation_events BEGIN SELECT RAISE(ABORT,'escalation_audit_fault'); END")
        actions = {
            'escalate': lambda: service.tools.escalate_issue('m1', issue['issue_id']),
            'claim': lambda: service.tools.claim_issue_escalation('m2', work_item),
            'resolve': lambda: service.tools.resolve_issue_escalation('m2', work_item, status='completed'),
        }
        if audit_fails:
            with pytest.raises(Exception, match='escalation_audit_fault'):
                actions[operation]()
            assert snapshot() == caller_state
        else:
            actions[operation]()
        assert db.in_transaction
        db.rollback()
        assert snapshot() == before
    finally:
        service.close()
