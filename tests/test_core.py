import pytest
from butian.core.models import TaskStatus
from butian.core.state_machine import InvalidTransition, transition
from butian.runtime.store import Store


def test_state_machine_rejects_done_to_running():
    with pytest.raises(InvalidTransition):
        transition(TaskStatus.DONE, TaskStatus.RUNNING)


def test_store_revision_and_idempotent_event(tmp_path):
    store = Store(tmp_path / "team.db")
    store.add_member("dispatcher", "派活者")
    session = store.create_session("team", "dispatcher", "model-a", {"can_create_child_session": False})
    task = store.create_task(session, "research")
    store.transition_task(task, 1, "ready", "op-1")
    with pytest.raises(ValueError, match="revision_conflict"):
        store.transition_task(task, 1, "claimed", "op-2")
    assert store.list_tasks()[0]["revision"] == 2
    store.close()


def test_evidence_hash(tmp_path):
    source = tmp_path / "evidence.txt"
    source.write_text("verified", encoding="utf-8")
    store = Store(tmp_path / "team.db")
    evidence_id = store.record_evidence(source)
    assert evidence_id.startswith("ev_")
    row = store.db.execute("select sha256, size, status from evidence").fetchone()
    assert row["size"] == 8 and row["status"] == "valid"
    store.close()


def test_claim_requires_member_and_release_requeues(tmp_path):
    store = Store(tmp_path / "team.db")
    store.add_member("worker", "执行者")
    session = store.create_session("team", "worker", None, {})
    task = store.create_task(session, "leased")
    store.transition_task(task, 1, "ready", "lease-ready")
    lease = store.claim_task(task, "worker")
    assert store.list_tasks()[0]["status"] == "claimed"
    store.release_lease(task, lease["lease_id"])
    row = store.list_tasks()[0]
    assert row["status"] == "ready" and row["lease_id"] is None
    with pytest.raises(ValueError, match="member_authentication_failed"):
        store.claim_task(task, "missing")
    store.close()


def test_expired_lease_is_reclaimed_and_reclaim_api_recovers_running(tmp_path):
    store = Store(tmp_path / "team.db")
    store.add_member("worker", "执行者")
    session = store.create_session("team", "worker", None, {})
    task = store.create_task(session, "expired")
    store.transition_task(task, 1, "ready", "expired-ready")
    lease = store.claim_task(task, "worker", lease_seconds=-1)
    assert store.list_tasks()[0]["status"] == "claimed"
    assert store.reclaim_expired_leases(now=lease["lease_until"] + 1) == [task]
    assert store.list_tasks()[0]["status"] == "ready"
    second = store.claim_task(task, "worker")
    assert second["revision"] == lease["revision"] + 2
    store.close()


def test_claim_token_is_checked(tmp_path):
    store = Store(tmp_path / "team.db")
    store.add_member("worker", "执行者")
    store.set_member_token("worker", "secret")
    session = store.create_session("team", "worker", None, {})
    task = store.create_task(session, "protected")
    store.transition_task(task, 1, "ready", "token-ready")
    with pytest.raises(ValueError, match="member_authentication_failed"):
        store.claim_task(task, "worker", token="wrong")
    lease = store.claim_task(task, "worker", token="secret")
    assert lease["lease_id"].startswith("lease_")
    store.close()

def test_cancel_running_task_enters_cancelling(tmp_path):
    store = Store(tmp_path / "team.db")
    store.add_member("worker", "执行者")
    session = store.create_session("team", "worker", None, {})
    task = store.create_task(session, "cancel")
    store.transition_task(task, 1, "ready", "cancel-ready")
    lease = store.claim_task(task, "worker")
    store.transition_task(task, lease["revision"], "running", "cancel-running")
    store.cancel_task(task, lease["revision"] + 1, "cancel-request")
    assert store.list_tasks()[0]["status"] == "cancelling"
    store.close()


def test_generic_provider_session_outputs_are_idempotent_and_terminal(tmp_path):
    from butian.runtime.store import Store
    store = Store(tmp_path / "team.db")
    store.add_member("m", "成员", provider_id="generic-agent")
    session_id = store.create_provider_session("m", "generic-agent", task_id="task-x")
    assert store.record_provider_output(session_id, "event-1", "partial") is True
    assert store.record_provider_output(session_id, "event-1", "duplicate") is False
    assert store.provider_session(session_id)["last_provider_event_id"] == "event-1"
    assert store.finish_provider_session(session_id, "completed")["status"] == "completed"
    with pytest.raises(ValueError, match="provider_session_terminal"):
        store.record_provider_output(session_id, "event-2", "late")
    with pytest.raises(ValueError, match="invalid_provider_session_transition"):
        store.finish_provider_session(session_id, "running")
    store.close()


def test_recover_replays_prepared_task_transition(tmp_path):
    from butian.runtime.store import Store
    store = Store(tmp_path / "team.db")
    session = store.create_session("solo")
    task_id = store.create_task(session, "recover")
    store.transition_task(task_id, 1, "ready", "prepare-recover")
    row = store.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    operation_id = "op_crash_replay"
    event_id = "evt_crash_replay"
    store.db.execute("INSERT INTO operations VALUES (?,?,?,?,?)", (operation_id, "crash-replay", "prepared", 1.0, None))
    store.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", (event_id, operation_id, "crash-replay", task_id, "task_transition", '{"from":"ready","to":"running","revision":3}', 999, 1.0, "prepared"))
    result = store.recover()
    assert result[0]["recovery"] == "replayed"
    task = store.db.execute("SELECT status, revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    assert (task["status"], task["revision"]) == ("running", 3)
    assert store.db.execute("SELECT state FROM operations WHERE operation_id=?", (operation_id,)).fetchone()[0] == "committed"
    store.close()


def test_recover_commits_already_applied_transition(tmp_path):
    from butian.runtime.store import Store
    store = Store(tmp_path / "team.db")
    session = store.create_session("solo")
    task_id = store.create_task(session, "already")
    store.transition_task(task_id, 1, "ready", "prepare-already")
    store.db.execute("UPDATE tasks SET status='running', revision=2 WHERE task_id=?", (task_id,))
    operation_id = "op_crash_applied"
    store.db.execute("INSERT INTO operations VALUES (?,?,?,?,?)", (operation_id, "crash-applied", "prepared", 1.0, None))
    store.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", ("evt_crash_applied", operation_id, "crash-applied", task_id, "task_transition", '{"from":"ready","to":"running","revision":2}', 999, 1.0, "prepared"))
    assert store.recover()[0]["recovery"] == "already_applied"
    store.close()


def test_store_concurrent_idempotent_events(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = Store(tmp_path / "team.db")
    try:
        def write_event(_):
            return store.append_event("member", "concurrent_test", {"ok": True}, "same-operation")
        with ThreadPoolExecutor(max_workers=8) as pool:
            event_ids = list(pool.map(write_event, range(32)))
        assert len(set(event_ids)) == 1
        assert store.db.execute("SELECT COUNT(*) FROM events WHERE idempotency_key='same-operation'").fetchone()[0] == 1
    finally:
        store.close()


def test_cancel_and_provider_review_race_has_one_atomic_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = Store(tmp_path / "race.db")
    try:
        session = store.create_session("solo", None, None, {})
        task_id = store.create_task(session, "race")
        store.transition_task(task_id, 1, "ready", "race-ready")
        # Seed a running task without involving provider threads; both terminal
        # paths use the same revision-checked transition contract.
        row = store.db.execute("SELECT revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        store.transition_task(task_id, row["revision"], "claimed", "race-claimed")
        row = store.db.execute("SELECT revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        store.transition_task(task_id, row["revision"], "running", "race-running")
        revision = store.db.execute("SELECT revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()["revision"]

        def attempt(target, key):
            try:
                store.transition_task(task_id, revision, target, key)
                return "ok"
            except ValueError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: attempt(*pair), [("review", "race-review"), ("cancelling", "race-cancel")]))
        assert results.count("ok") == 1
        row = store.db.execute("SELECT status, revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        assert row["status"] in {"review", "cancelling"}
        assert row["revision"] == revision + 1
        assert store.db.execute("SELECT COUNT(*) FROM operations WHERE idempotency_key IN ('race-review','race-cancel')").fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM events WHERE aggregate_id=? AND commit_state='committed'", (task_id,)).fetchone()[0] == 4
    finally:
        store.close()
