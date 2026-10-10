import pytest
from butian.core.math_models import *


def test_bayes_is_bounded_and_diminishing():
    belief = BetaBelief()
    first = bayesian_evidence_update(belief, outcome="success")
    later = bayesian_evidence_update(first, outcome="success")
    assert 0 < first.mean < later.mean < 1
    assert first.interval()[0] < first.mean < first.interval()[1]
    assert later.effective_samples == 2


def test_bayes_rejects_fake_zero_weight_evidence():
    with pytest.raises(ValueError):
        bayesian_evidence_update(BetaBelief(), outcome="success", evidence_quality=0)


def test_information_gain_and_action_cost():
    prior = [0.5, 0.5]
    certain = [1.0, 0.0]
    assert information_gain(prior, certain) == pytest.approx(1.0)
    scores = score_information_action({"cheap": (prior, certain), "expensive": (prior, certain)}, costs={"cheap": 0, "expensive": 2}, cost_weight=.6)
    assert scores["cheap"] > scores["expensive"]


def test_markov_normalization_has_no_hidden_transition():
    assert transition_distribution("stable", {"activating": 2, "stable": 1}) == {"activating": pytest.approx(2/3), "stable": pytest.approx(1/3)}
    with pytest.raises(ValueError):
        transition_distribution("stable", {})


def test_causal_estimate_requires_both_groups_and_preserves_strata_data():
    result = stratified_effect(
        {"easy": ([1, 1], [0, 1]), "hard": ([0], [0])},
        weights={"easy": 0.5, "hard": 0.5},
    )
    assert result.effect == pytest.approx(((1 + 0) / 2) - ((.5 + 0) / 2))
    with pytest.raises(ValueError):
        difference_in_means([], [0])


def test_utility_queue_and_bandit_guards():
    options = {"a": Utility(1, 1, 0, 0, 0), "b": Utility(.8, .1, 0, 0, 0)}
    assert allocate_by_utility(options, budget=.2) == ["b"]
    assert queue_metrics(2, 4)["status"] == "stable"
    assert queue_metrics(4, 4)["status"] == "unstable"
    bandit = ThompsonBandit(["a", "b"], seed=3)
    chosen = bandit.choose()
    bandit.observe(chosen, 1)
    with pytest.raises(ValueError):
        bandit.observe("missing", 1)


def test_expected_information_gain_requires_outcome_model():
    assert expected_information_gain([.5, .5], [[1, 0], [0, 1]], [.5, .5]) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="outcome_probabilities_must_sum_to_one"):
        expected_information_gain([.5, .5], [[1, 0], [0, 1]], [.4, .4])


def test_budget_allocation_finds_best_combination_not_greedy():
    options = {
        "large": Utility(8, 1, 0, 0, 0),
        "a": Utility(5, .5, 0, 0, 0),
        "b": Utility(5, .5, 0, 0, 0),
    }
    assert set(allocate_by_utility(options, budget=1.0)) == {"a", "b"}


def test_beta_interval_uses_posterior_quantiles_not_prior_as_observations():
    prior = BetaBelief()
    assert prior.interval() == pytest.approx((.025, .975), abs=1e-6)
    assert prior.effective_samples == 0
    assert prior.prior_strength == 2
    posterior = BetaBelief(alpha=2, beta=2, observations=2)
    assert posterior.interval() == pytest.approx((.094299, .905701), abs=2e-5)


def test_information_gain_rejects_impossible_posterior_model():
    with pytest.raises(ValueError, match="posterior_violates_bayes_consistency"):
        expected_information_gain([.5, .5], [[1, 0], [0, 1]], [.75, .25])
    with pytest.raises(ValueError, match="posterior_entropy_exceeds_prior"):
        information_gain([1, 0], [.5, .5])


def test_budget_allocation_never_rounds_cost_down_or_selects_negative_utility():
    with pytest.raises(ValueError, match="utility_weights_must_be_nonnegative"):
        Utility(1, 0, 0, 0, 0).score(quality_weight=float("inf"))
    tiny = Utility(1, .1004, 0, 0, 0)
    assert allocate_by_utility({"tiny": tiny}, budget=.1) == []
    assert allocate_by_utility({"loss": Utility(0, .1, 0, 0, 0)}, budget=None) == []
    assert allocate_by_utility({"free": Utility(1, 0, 0, 0, 0)}, budget=0) == ["free"]


def test_stratification_and_rate_models_reject_invalid_numeric_inputs():
    with pytest.raises(ValueError, match="empty_stratum_group"):
        stratified_effect({"empty": ([], [1])}, weights={"empty": 1.0})
    with pytest.raises(ValueError, match="outcome_out_of_range"):
        difference_in_means([float("nan")], [0])
    with pytest.raises(ValueError, match="invalid_queue_rates"):
        queue_metrics(float("inf"), 1)


def test_bernoulli_thompson_bandit_rejects_fractional_reward():
    with pytest.raises(ValueError, match="bernoulli_bandit_requires_binary_reward"):
        BanditArm().observe(.5)
