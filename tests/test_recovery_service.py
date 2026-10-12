import pytest

from butian.runtime.service import ButianService


def test_service_restart_recovers_prepared_operation_and_reclaims_lease(tmp_path):
    home = tmp_path / "runtime"
    first = ButianService(home)
    session = first.store.create_session("solo", None, None, {})
    recovery_task_id = first.store.create_task(session, "restart recovery")
    first.store.transition_task(recovery_task_id, 1, "ready", "seed-ready")
    first.store.db.execute("INSERT INTO operations VALUES (?,?,?,?,?)", ("op_restart", "restart", "prepared", 1.0, None))
    first.store.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", ("evt_restart", "op_restart", "restart", recovery_task_id, "task_transition", '{"from":"ready","to":"running","revision":3}', 999, 1.0, "prepared"))

    first.add_member("member", "Member")
    lease_task_id = first.store.create_task(session, "expired lease")
    first.store.transition_task(lease_task_id, 1, "ready", "seed-lease")
    first.store.claim_task(lease_task_id, "member", lease_seconds=1)
    first.store.db.execute("UPDATE tasks SET lease_until=? WHERE task_id=?", (0.0, lease_task_id))
    first.close()

    second = ButianService(home)
    try:
        assert second.startup_recovery["recovered_operations"][0]["recovery"] == "replayed"
        assert lease_task_id in second.startup_recovery["reclaimed_task_ids"]
        recovered = second.store.db.execute("SELECT status, revision FROM tasks WHERE task_id=?", (recovery_task_id,)).fetchone()
        assert (recovered["status"], recovered["revision"]) == ("running", 3)
        reclaimed = second.store.db.execute("SELECT status, lease_id FROM tasks WHERE task_id=?", (lease_task_id,)).fetchone()
        assert (reclaimed["status"], reclaimed["lease_id"]) == ("ready", None)
    finally:
        second.close()


def test_task_cancel_without_provider_reaches_cancelled(tmp_path):
    service = ButianService(tmp_path / "cancel-runtime")
    try:
        service.add_member("member", "Member")
        session = service.store.create_session("solo", None, None, {})
        task_id = service.store.create_task(session, "cancel me", assignee_id="member")
        service.store.transition_task(task_id, 1, "ready", "cancel-ready")
        lease = service.store.claim_task(task_id, "member")
        service.store.transition_task(task_id, lease["revision"], "running", "cancel-running")
        result = service.cancel_task(task_id, lease["revision"] + 1)
        assert result["status"] == "cancelled"
        assert service.store.db.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()["status"] == "cancelled"
    finally:
        service.close()


def test_task_heartbeat_extends_valid_lease(tmp_path):
    service = ButianService(tmp_path / "heartbeat-runtime")
    try:
        service.add_member("member", "Member")
        session = service.store.create_session("solo", None, None, {})
        task_id = service.store.create_task(session, "heartbeat", assignee_id="member")
        service.store.transition_task(task_id, 1, "ready", "heartbeat-ready")
        lease = service.store.claim_task(task_id, "member", lease_seconds=1)
        service.store.heartbeat(task_id, lease["lease_id"], lease_seconds=300)
        row = service.store.db.execute("SELECT lease_until FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        assert row["lease_until"] > lease["lease_until"]
    finally:
        service.close()


def test_provider_cancel_unsupported_preserves_session_and_task_cancelling(tmp_path):
    from butian.adapters.base import ProviderResult
    from butian.adapters.registry import ProviderRegistry

    class NoCancelProvider:
        provider_id = "no-cancel"
        def cancel(self, provider_session_id):
            return ProviderResult("unsupported", reason="provider_has_no_cancel")

    service = ButianService(tmp_path / "unsupported-cancel")
    try:
        service.add_member("member", "Member")
        provider = NoCancelProvider()
        service.providers.register(provider)
        session = service.store.create_session("solo", None, None, {})
        task_id = service.store.create_task(session, "cancel provider work", assignee_id="member")
        service.store.transition_task(task_id, 1, "ready", "unsupported-ready")
        lease = service.store.claim_task(task_id, "member")
        service.store.transition_task(task_id, lease["revision"], "running", "unsupported-running")
        provider_session_id = service.store.create_provider_session("member", provider.provider_id, task_id=task_id)
        result = service.cancel_task(task_id, lease["revision"] + 1)
        assert result["status"] == "cancelling"
        assert result["provider"]["status"] == "unsupported"
        assert service.store.provider_session(provider_session_id)["status"] == "running"
    finally:
        service.close()

def test_cancel_terminal_provider_session_is_idempotent(tmp_path):
    service = ButianService(tmp_path / "terminal-cancel")
    try:
        service.add_member("member", "Member")
        provider_session_id = service.store.create_provider_session("member", "manual")
        service.store.finish_provider_session(provider_session_id, "cancelled", "already_cancelled")
        result = service.cancel_provider_task(provider_session_id)
        assert result == {"provider_session_id": provider_session_id, "status": "cancelled", "reason": "already_cancelled"}
    finally:
        service.close()

def test_team_child_lease_is_released_when_child_enters_review(tmp_path):
    service = ButianService(tmp_path / "team-lease")
    try:
        service.add_member("dispatcher", "Dispatcher")
        from butian.adapters.base import ProviderEvent, ProviderResult
        class CompletingProvider:
            provider_id = "completing"
            def capabilities(self):
                from butian.core.models import ProviderCapabilities
                return ProviderCapabilities(can_create_child_session=True, can_stream_output=True)
            def models(self): return ["test"]
            def select_model(self, requested_model):
                from butian.core.models import ModelSelection
                return ModelSelection(requested_model, "test", "applied")
            def create_child_session(self, member_id, prompt, model=None):
                return ProviderResult("accepted", provider_event_id="remote-test", model="test")
            def stream(self, provider_session_id, prompt):
                yield ProviderEvent("output", "output", "completed work")
                yield ProviderEvent("done", "status", status="completed")
            def cancel(self, provider_session_id): return ProviderResult("unsupported", reason="not_supported")
        service.providers.register(CompletingProvider())
        service.add_member("worker", "Worker", provider_id="completing", model_id="test")
        result = service.submit_goal("goal", mode="team", dispatcher_member_id="dispatcher", member_ids=["worker"], subtasks=["do work"])
        service.run_team_goal(result["task_id"])
        child = service.store.db.execute("SELECT status, lease_id, lease_until FROM tasks WHERE parent_id=?", (result["task_id"],)).fetchone()
        assert child["status"] == "review"
        assert child["lease_id"] is None
        assert child["lease_until"] is None
    finally:
        service.close()


def test_service_close_does_not_close_database_when_executions_remain(tmp_path, monkeypatch):
    service = ButianService(tmp_path)
    try:
        monkeypatch.setattr(service.tools, "wait_for_executions", lambda timeout=10.0: False)
        original_close = service.store.close
        closed = []
        monkeypatch.setattr(service.store, "close", lambda: closed.append(True))
        with pytest.raises(RuntimeError, match="shutdown_incomplete_active_tool_executions"):
            service.close()
        assert closed == []
    finally:
        if not closed:
            monkeypatch.setattr(service.tools, "wait_for_executions", lambda timeout=10.0: True)
            original_close()


def test_stale_rework_does_not_increment_count(tmp_path):
    svc = ButianService(tmp_path)
    try:
        session = svc.store.create_session('solo', None, None, {})
        task = svc.store.create_task(session, 'stale review')
        for revision, state in enumerate(['ready', 'claimed', 'running', 'review'], 1):
            svc.store.transition_task(task, revision, state, f'stale:{state}')
        with pytest.raises(ValueError):
            svc.review_task(task, 4, 'rework')
        row = svc.store.db.execute('SELECT rework_count,status FROM tasks WHERE task_id=?', (task,)).fetchone()
        assert row['rework_count'] == 0
        assert row['status'] == 'review'
    finally:
        svc.close()


def test_close_refuses_live_maintenance_before_closing_database(tmp_path):
    svc = ButianService(tmp_path)
    original = svc._maintenance_thread
    if original:
        svc._maintenance_stop.set()
        original.join(3)
    class LiveMaintenance:
        def join(self, timeout):
            pass
        def is_alive(self):
            return True
    svc._maintenance_thread = LiveMaintenance()
    try:
        with pytest.raises(RuntimeError, match='shutdown_incomplete_active_maintenance'):
            svc.close()
        assert svc.store.db.execute('SELECT 1').fetchone()[0] == 1
    finally:
        svc._maintenance_thread = None
        svc.close()


def test_provider_start_rejection_releases_running_task(tmp_path):
    import json
    import sys
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': [sys.executable, '-c', 'print(1)']}]}), encoding='utf-8')
    svc = ButianService(tmp_path)
    try:
        svc.add_member('worker', 'Worker', provider_id='local-command')
        session = svc.store.create_session('solo', 'worker', None, {})
        task = svc.store.create_task(session, 'rejected start', assignee_id='worker')
        svc.store.transition_task(task, 1, 'ready', 'start-ready')
        lease = svc.store.claim_task(task, 'worker', internal=True)
        svc.store.transition_task(task, lease['revision'], 'running', 'start-running')
        result = svc.start_provider_task('worker', 'work', task_id=task, model='unavailable-model')
        assert result['status'] == 'unsupported'
        row = svc.store.db.execute('SELECT status,lease_id,payload FROM tasks WHERE task_id=?', (task,)).fetchone()
        assert row['status'] == 'unknown'
        assert row['lease_id'] is None
        assert json.loads(row['payload'])['reason']
    finally:
        svc.close()


def test_rework_transition_failure_rolls_back_count(tmp_path):
    import sqlite3
    svc = ButianService(tmp_path)
    try:
        session = svc.store.create_session('solo', None, None, {})
        task = svc.store.create_task(session, 'transaction failure')
        for revision, state in enumerate(['ready', 'claimed', 'running', 'review'], 1):
            svc.store.transition_task(task, revision, state, f'rollback:{state}')
        svc.store.db.execute("CREATE TRIGGER reject_rework BEFORE UPDATE OF status ON tasks WHEN NEW.status='ready' BEGIN SELECT RAISE(ABORT, 'forced_transition_failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match='forced_transition_failure'):
            svc.review_task(task, 5, 'rework')
        row = svc.store.db.execute('SELECT rework_count,status FROM tasks WHERE task_id=?', (task,)).fetchone()
        assert row['rework_count'] == 0
        assert row['status'] == 'review'
    finally:
        svc.close()
