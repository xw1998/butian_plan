import math

import pytest

from butian.core.degradation import multiplicative_freshness
from butian.core.experience import composition_gain, decision_value, saturated_strength, SkillPhase
from butian.core.math_models import transition_distribution


@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize('field', ['age_days', 'half_life_days', 'consecutive_failures', 'contradiction_count'])
def test_freshness_rejects_nonfinite_inputs(field, bad):
    values = dict(age_days=1, half_life_days=30, consecutive_failures=0, contradiction_count=0)
    values[field] = bad
    with pytest.raises(ValueError):
        multiplicative_freshness(**values)


@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf])
def test_transition_scores_reject_nonfinite_mass(bad):
    with pytest.raises(ValueError):
        transition_distribution('candidate', {'warm': bad, 'candidate': 1})


@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf])
def test_strength_rejects_nonfinite_parameters(bad):
    with pytest.raises(ValueError):
        saturated_strength(1, base=bad)


@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf])
def test_composition_rejects_nonfinite_values(bad):
    with pytest.raises(ValueError):
        composition_gain([1, bad])


@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf])
def test_decision_rejects_nonfinite_strength(bad):
    with pytest.raises(ValueError):
        decision_value(bad, 1, 1, 1, 1, SkillPhase.DEVELOPING)

@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize('field', ['successes', 'base', 'gain'])
def test_strength_rejects_each_nonfinite_parameter(field, bad):
    values = dict(successes=1, base=1, gain=1)
    values[field] = bad
    with pytest.raises(ValueError, match='strength_parameters_must_be_nonnegative'):
        saturated_strength(**values)

@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize('field', ['composition_bonus', 'redundancy_penalty', 'conflict_penalty'])
def test_composition_rejects_nonfinite_adjustments(field, bad):
    with pytest.raises(ValueError, match='composition_values_must_be_nonnegative'):
        composition_gain([1], **{field: bad})

@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize('field', ['environment_match', 'recall_quality', 'composition_quality', 'evidence_confidence', 'conflict_penalty', 'transfer_risk'])
def test_decision_rejects_each_nonfinite_factor(field, bad):
    values = dict(strength=1, environment_match=1, recall_quality=1,
                  composition_quality=1, evidence_confidence=1,
                  phase=SkillPhase.DEVELOPING, conflict_penalty=0, transfer_risk=0)
    values[field] = bad
    with pytest.raises(ValueError, match='decision_values_out_of_range'):
        decision_value(**values)

def test_transition_rejects_overflowed_total_mass():
    with pytest.raises(ValueError, match='invalid_transition_scores'):
        transition_distribution('candidate', {'candidate': 1e308, 'warm': 1e308})

def test_finite_transition_remains_normalized():
    distribution = transition_distribution('candidate', {'candidate': 2, 'warm': 3})
    assert distribution == pytest.approx({'candidate': .4, 'warm': .6})
