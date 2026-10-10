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
