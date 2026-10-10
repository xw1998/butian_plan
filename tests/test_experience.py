import pytest
from butian.core.experience import *

def test_strength_is_monotonic_and_saturating():
    values = [saturated_strength(i) for i in range(20)]
    assert all(a <= b for a, b in zip(values, values[1:]))
    assert values[-1] - values[-2] < values[1] - values[0]

def test_phase_curve_is_bounded_and_monotonic():
    for phase in SkillPhase:
        values = [phase_curve(i / 10, phase) for i in range(11)]
        assert values[0] == 0 and values[-1] == pytest.approx(1)
        assert all(a <= b for a, b in zip(values, values[1:]))

def test_spiral_requires_evidence_for_each_jump():
    state = SkillState(SkillPhase.STABLE, .7)
    assert transition_phase(state, marginal=.2).phase == SkillPhase.STABLE
    active = transition_phase(state, marginal=.2, novel_environment=True)
    assert active.phase == SkillPhase.ACTIVATING and active.activation_count == 1
    assert transition_phase(active, marginal=.2, independent_successes=1).phase == SkillPhase.ACTIVATING
    assert transition_phase(active, marginal=.2, independent_successes=2).phase == SkillPhase.INTEGRATING

def test_composition_and_decision_are_bounded():
    assert composition_gain([.4, .3], composition_bonus=.2) == pytest.approx(.9)
    assert decision_value(1, 1, 1, 1, 1, SkillPhase.TRANSCENDING) == 1
    assert decision_value(1, 1, 1, 1, 1, SkillPhase.DEVELOPING, conflict_penalty=2) == 0

def test_invalid_values_rejected():
    with pytest.raises(ValueError): saturated_strength(-1)
    with pytest.raises(ValueError): phase_curve(2, SkillPhase.STABLE)
