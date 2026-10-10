import pytest
from butian.core.math_models import *


def test_bayes_is_bounded_and_diminishing():
    belief = BetaBelief()
    first = bayesian_evidence_update(belief, outcome="success")
    later = bayesian_evidence_update(first, outcome="success")
    assert 0 < first.mean < later.mean < 1
    assert first.interval()[0] < first.mean < first.interval()[1]
    assert later.effective_samples == 4


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
    result = stratified_effect({"easy": ([1, 1], [0, 1]), "hard": ([0], [0])})
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
