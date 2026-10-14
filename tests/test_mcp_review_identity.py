import pytest
from butian.mcp_server import call_tool
from butian.runtime.service import ButianService


def test_mcp_review_defaults_to_authenticated_actor(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('owner', 'Owner')
        service.store.set_member_token('owner', 'test-owner')
        session = service.create_session('solo', 'owner', current_member_id='owner')
        task = service.store.create_task(session['session_id'], 'Review', 'owner')
        captured = {}
        def review(*args, **kwargs):
            captured.update(kwargs)
            return {'ok': True}
        monkeypatch.setattr(service, 'review_task', review)
        call_tool(service, 'butian_task_review', dict(task_id=task, revision=1, verdict='rework', actor_member_id='owner', token='test-owner'))
        assert captured['actor_member_id'] == 'owner'
        assert captured['reviewer_member_id'] == 'owner'
    finally:
        service.close()


def test_mcp_review_rejects_spoofed_reviewer(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('owner', 'Owner')
        service.add_member('other', 'Other')
        service.store.set_member_token('owner', 'test-owner')
        session = service.create_session('solo', 'owner', current_member_id='owner')
        task = service.store.create_task(session['session_id'], 'Review', 'owner')
        with pytest.raises(ValueError, match='reviewer_identity_mismatch'):
            call_tool(service, 'butian_task_review', dict(task_id=task, revision=1, verdict='rework', actor_member_id='owner', token='test-owner', reviewer_member_id='other'))
        assert service.store.db.execute('SELECT revision FROM tasks WHERE task_id=?', (task,)).fetchone()[0] == 1
    finally:
        service.close()


def test_dispatcher_cannot_impersonate_authorized_assignee_reviewer(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('dispatcher', 'Dispatcher')
        service.add_member('worker', 'Worker')
        service.store.set_member_token('dispatcher', 'test-dispatcher')
        session = service.create_session('team', 'dispatcher', current_member_id='dispatcher')
        task = service.store.create_task(session['session_id'], 'Review', 'worker')
        for revision, status in enumerate(['ready', 'claimed', 'running', 'review'], 1):
            service.store.transition_task(task, revision, status, 'identity-' + status)
        with pytest.raises(ValueError, match='reviewer_identity_mismatch'):
            call_tool(service, 'butian_task_review', dict(task_id=task, revision=5, verdict='rework', actor_member_id='dispatcher', token='test-dispatcher', reviewer_member_id='worker'))
        row = service.store.db.execute('SELECT status,revision FROM tasks WHERE task_id=?', (task,)).fetchone()
        assert tuple(row) == ('review', 5)
    finally:
        service.close()


def test_review_rechecks_dispatcher_inside_transition(tmp_path, monkeypatch):
    service = ButianService(tmp_path)
    other = ButianService(tmp_path)
    try:
        for member in ('dispatcher', 'worker', 'replacement'):
            service.add_member(member, member)
        session = service.create_session('team', 'dispatcher', current_member_id='dispatcher')
        task = service.store.create_task(session['session_id'], 'Review', 'worker')
        for revision, status in enumerate(['ready', 'claimed', 'running', 'review'], 1):
            service.store.transition_task(task, revision, status, 'race-' + status)
        original = service.store.transition_task
        def revoke_before_transition(*args, **kwargs):
            other.store.db.execute('UPDATE sessions SET dispatcher_member_id=? WHERE session_id=?', ('replacement', session['session_id']))
            return original(*args, **kwargs)
        monkeypatch.setattr(service.store, 'transition_task', revoke_before_transition)
        with pytest.raises(ValueError, match='reviewer_not_authorized'):
            service.review_task(task, 5, 'rework', reviewer_member_id='dispatcher', actor_member_id='dispatcher')
        row = other.store.db.execute('SELECT status,revision,rework_count FROM tasks WHERE task_id=?', (task,)).fetchone()
        assert tuple(row) == ('review', 5, 0)
    finally:
        other.close()
        service.close()
