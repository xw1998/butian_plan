import pytest
from butian.core.models import TaskStatus
from butian.core.state_machine import InvalidTransition, transition
from butian.runtime.store import Store


def test_state_machine_rejects_done_to_running():
    with pytest.raises(InvalidTransition):
        transition(TaskStatus.DONE, TaskStatus.RUNNING)


def test_store_revision_and_idempotent_event(tmp_path):
    store = Store(tmp_path / "team.db")
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
