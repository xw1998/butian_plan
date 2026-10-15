import math
import pytest
from butian.core.math_models import expected_information_gain


def test_rejects_inconsistent_mixture_despite_elementwise_bounds():
    with pytest.raises(ValueError, match='posterior_mixture_must_equal_prior'):
        expected_information_gain([0.5, 0.5], [[0.8, 0.2], [0.8, 0.2]], [0.5, 0.5])


def test_rejects_mismatched_state_spaces():
    with pytest.raises(ValueError, match='posterior_state_length_mismatch'):
        expected_information_gain([0.5, 0.5], [[1.0]], [1.0])


@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf])
def test_rejects_nonfinite_outcome_mass(value):
    with pytest.raises(ValueError, match='outcome_probability_out_of_range'):
        expected_information_gain([0.5, 0.5], [[0.5, 0.5], [0.5, 0.5]], [value, 0.5])


def test_perfect_observation_has_one_bit_information():
    assert expected_information_gain([0.5, 0.5], [[1, 0], [0, 1]], [0.5, 0.5]) == pytest.approx(1)


def test_uninformative_observation_has_zero_information():
    assert expected_information_gain([0.3, 0.7], [[0.3, 0.7], [0.3, 0.7]], [0.2, 0.8]) == pytest.approx(0)
