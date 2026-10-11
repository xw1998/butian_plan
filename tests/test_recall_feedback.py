from butian.runtime.service import ButianService


def test_recall_feedback_only_marks_its_own_decision(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("m1", "Member")
    memory = service.memory("m1")
    memory.put("mem1", "lesson", "decision evidence recall isolation", confidence=.8, source_event_ids=["evt_test"], status="warm")
    memory.close()

    first = service.recall_for_decision("m1", "decision evidence", decision_id="decision-1")
    second = service.recall_for_decision("m1", "decision evidence", decision_id="decision-2")
    service.record_recall_outcome("m1", first["recall_id"], used_memory_ids=["mem1"], missed=False)

    memory = service.memory("m1")
    try:
        rows = list(memory.db.execute("SELECT decision_id, used FROM memory_recall_log WHERE layer='decision' ORDER BY created_at"))
        assert [(row["decision_id"], row["used"]) for row in rows] == [("decision-1", 1), ("decision-2", 0)]
    finally:
        memory.close()
        service.close()


def test_recall_feedback_rejects_unrecalled_and_missing_evidence(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("m1", "Member")
    memory = service.memory("m1")
    memory.put("mem1", "lesson", "feedback boundary", confidence=.8, source_event_ids=["evt_test"], status="warm")
    memory.close()
    recall = service.recall_for_decision("m1", "feedback boundary", decision_id="d1")
    try:
        service.record_recall_outcome("m1", recall["recall_id"], used_memory_ids=["unknown"], missed=False)
    except ValueError as exc:
        assert str(exc) == "feedback_memory_not_in_recall"
    else:
        raise AssertionError("unrecalled memory accepted")
    try:
        service.record_recall_outcome("m1", recall["recall_id"], used_memory_ids=["mem1"], useful_memory_ids=["mem1"], missed=False)
    except ValueError as exc:
        assert str(exc) == "usage_feedback_evidence_required"
    else:
        raise AssertionError("feedback without evidence accepted")
    service.close()
