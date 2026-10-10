import pytest
from butian.core.degradation import *
from butian.core.experience import SkillPhase, SkillState, transition_phase
from butian.runtime.memory import MemoryStore


def test_degradation_is_bounded_and_monotonic():
    low = degradation_score(age_days=0, half_life_days=30)
    old = degradation_score(age_days=30, half_life_days=30)
    failed = degradation_score(age_days=30, half_life_days=30, consecutive_failures=2)
    assert low == pytest.approx(0)
    assert 0 < old < failed <= 1
    assert degradation_state(old) == "degraded"
    assert degradation_state(.2) == "cooling"
    assert degradation_state(.45) == "degraded"
    assert degradation_state(.7) == "disputed"


def test_environment_drift_ignores_unknown_keys_and_detects_shared_changes():
    assert environment_drift({"os": "x"}, {"os": "y", "new": 1}) == 1
    assert environment_drift({"os": "x"}, {"new": 1}) == 0


def test_regression_requires_explicit_contradiction_and_evidence():
    state = SkillState(SkillPhase.TRANSCENDING, 0.9, 2)
    assert transition_phase(state, marginal=0.1, contradiction=True).phase == SkillPhase.TRANSCENDING
    assert transition_phase(state, marginal=0.1, contradiction=True, regression_evidence=True).phase == SkillPhase.ACTIVATING


def test_memory_degradation_is_persisted_and_does_not_invalidate_by_score_alone(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.put("m", "lesson", "验证退化", confidence=1, weight=1, environment={"os": "a"}, source_event_ids=["e"], status="warm")
    result = store.assess_memory_degradation("m", now=31 * 86400, half_life_days=30, current_environment={"os": "b"})
    assert result.state in {"cooling", "degraded", "disputed"}
    row = store.db.execute("select * from memory_degradation where memory_id='m'").fetchone()
    memory = store.db.execute("select status, confidence, weight from memories where memory_id='m'").fetchone()
    assert row and memory["status"] != "invalidated"
    first_weight = memory["weight"]
    store.assess_memory_degradation("m", now=31 * 86400, half_life_days=30, current_environment={"os": "b"})
    second_weight = store.db.execute("select weight from memories where memory_id='m'").fetchone()[0]
    assert second_weight == pytest.approx(first_weight)
    store.close()

def test_lesson_uses_same_degradation_counterevidence_and_rewarm_gates(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.put("lesson", "lesson", "版本不匹配时不可推定兼容", confidence=1.0, weight=1.0, environment={"python": "3.11", "library": "1.0"}, source_event_ids=["evt:verified"], status="warm")
    degraded = store.assess_memory_degradation("lesson", now=31 * 86400, half_life_days=30, current_environment={"python": "3.12", "library": "2.0"}, consecutive_failures=2, contradiction_count=1)
    assert degraded.score > 0
    row = store.db.execute("SELECT status, weight FROM memories WHERE memory_id='lesson'").fetchone()
    assert row["status"] == "disputed" and row["weight"] < 1.0
    store.put("lesson", "lesson", "修正：版本不匹配需先做兼容性实测", confidence=.4, weight=.3, environment={"python": "3.12", "library": "2.0"}, source_event_ids=["evt:counterexample"], status="candidate", change_reason="新环境反证，重写适用条件")
    assert store.list_versions("lesson")[-1]["version_status"] == "candidate"
    store.db.execute("UPDATE memories SET status='cold' WHERE memory_id='lesson'")
    with pytest.raises(ValueError, match="evidence_required_for_rewarm"):
        store.rewarm("lesson", success=True)
    assert store.rewarm("lesson", success=True, evidence_ref="evt:reproduced") == "warm"
    store.audit_version_chain("lesson")
    store.close()
