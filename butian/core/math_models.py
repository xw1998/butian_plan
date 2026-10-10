from __future__ import annotations

from dataclasses import dataclass
from math import log, sqrt
from random import Random
from statistics import NormalDist
from typing import Iterable, Mapping, Sequence


def _unit(value: float, name: str) -> float:
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name}_out_of_range")
    return value


@dataclass(frozen=True)
class BetaBelief:
    alpha: float = 1.0
    beta: float = 1.0

    def __post_init__(self) -> None:
        if self.alpha <= 0 or self.beta <= 0:
            raise ValueError("beta_parameters_must_be_positive")

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def effective_samples(self) -> float:
        return self.alpha + self.beta

    def update(self, success_weight: float = 0.0, failure_weight: float = 0.0) -> "BetaBelief":
        if success_weight < 0 or failure_weight < 0:
            raise ValueError("evidence_weights_must_be_nonnegative")
        if success_weight == 0 and failure_weight == 0:
            raise ValueError("evidence_weight_required")
        return BetaBelief(self.alpha + success_weight, self.beta + failure_weight)

    def interval(self, confidence: float = 0.95) -> tuple[float, float]:
        _unit(confidence, "confidence")
        if confidence <= 0:
            raise ValueError("confidence_must_be_positive")
        # Wilson interval is a conservative, dependency-free approximation.
        z = NormalDist().inv_cdf(0.5 + confidence / 2)
        n = self.effective_samples
        p = self.mean
        d = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / d
        radius = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
        return max(0.0, centre - radius), min(1.0, centre + radius)


def bayesian_evidence_update(belief: BetaBelief, *, outcome: str, evidence_quality: float = 1.0,
                             independence: float = 1.0, environment_match: float = 1.0) -> BetaBelief:
    if outcome not in {"success", "failure"}:
        raise ValueError("unknown_evidence_outcome")
    weight = _unit(evidence_quality, "evidence_quality") * _unit(independence, "independence") * _unit(environment_match, "environment_match")
    return belief.update(success_weight=weight if outcome == "success" else 0.0,
                         failure_weight=weight if outcome == "failure" else 0.0)


@dataclass(frozen=True)
class MarkovTransition:
    from_phase: str
    to_phase: str
    probability: float
    reason: str


def transition_distribution(current: str, candidates: Mapping[str, float], *, evidence: float = 1.0) -> dict[str, float]:
    """Normalize explicitly supplied transition scores; never invents a transition."""
    _unit(evidence, "evidence")
    if not candidates or any(value < 0 for value in candidates.values()):
        raise ValueError("invalid_transition_scores")
    total = sum(candidates.values())
    if total <= 0:
        raise ValueError("transition_scores_have_zero_mass")
    return {state: value / total for state, value in candidates.items()}


def entropy(probabilities: Iterable[float]) -> float:
    values = [_unit(value, "probability") for value in probabilities]
    total = sum(values)
    if not values or abs(total - 1.0) > 1e-9:
        raise ValueError("probabilities_must_sum_to_one")
    return -sum(value * log(value, 2) for value in values if value > 0)


def information_gain(prior: Sequence[float], conditional: Sequence[float]) -> float:
    """Information reduction for one explicitly supplied posterior."""
    return max(0.0, entropy(prior) - entropy(conditional))


def expected_information_gain(prior: Sequence[float], posteriors: Sequence[Sequence[float]], outcome_probabilities: Sequence[float]) -> float:
    """Expected entropy reduction across mutually exclusive observations.

    The caller must provide a normalized prior, outcome probabilities, and one
    normalized posterior per outcome. No outcome or posterior is inferred.
    """
    if len(posteriors) != len(outcome_probabilities) or not posteriors:
        raise ValueError("posterior_outcome_length_mismatch")
    raw_probabilities = [float(value) for value in outcome_probabilities]
    if any(value < 0 or value > 1 for value in raw_probabilities):
        raise ValueError("outcome_probability_out_of_range")
    if abs(sum(raw_probabilities) - 1.0) > 1e-9:
        raise ValueError("outcome_probabilities_must_sum_to_one")
    probabilities = raw_probabilities
    prior_entropy = entropy(prior)
    expected_posterior_entropy = sum(probability * entropy(posterior) for probability, posterior in zip(probabilities, posteriors))
    return max(0.0, prior_entropy - expected_posterior_entropy)


def score_information_action(actions: Mapping[str, tuple[Sequence[float], Sequence[float]]], *, cost_weight: float = 0.0,
                             costs: Mapping[str, float] | None = None) -> dict[str, float]:
    if cost_weight < 0:
        raise ValueError("cost_weight_must_be_nonnegative")
    costs = costs or {}
    scores = {}
    for name, (prior, conditional) in actions.items():
        cost = costs.get(name, 0.0)
        if cost < 0:
            raise ValueError("action_cost_must_be_nonnegative")
        scores[name] = information_gain(prior, conditional) - cost_weight * cost
    return scores


@dataclass(frozen=True)
class CausalEstimate:
    treated_mean: float
    control_mean: float
    effect: float
    treated_count: int
    control_count: int


def difference_in_means(treated: Sequence[float], control: Sequence[float]) -> CausalEstimate:
    if not treated or not control:
        raise ValueError("both_groups_required")
    if any(value < 0 or value > 1 for value in (*treated, *control)):
        raise ValueError("outcome_out_of_range")
    t, c = sum(treated) / len(treated), sum(control) / len(control)
    return CausalEstimate(t, c, t - c, len(treated), len(control))


def stratified_effect(strata: Mapping[str, tuple[Sequence[float], Sequence[float]]]) -> CausalEstimate:
    """Equal-stratum standardization; sample counts do not define the estimand."""
    if not strata:
        raise ValueError("strata_required")
    estimates = [difference_in_means(treated, control) for treated, control in strata.values()]
    treated_mean = sum(item.treated_mean for item in estimates) / len(estimates)
    control_mean = sum(item.control_mean for item in estimates) / len(estimates)
    return CausalEstimate(treated_mean, control_mean, treated_mean - control_mean,
                          sum(item.treated_count for item in estimates),
                          sum(item.control_count for item in estimates))


@dataclass(frozen=True)
class Utility:
    quality: float
    cost: float
    risk: float
    learning: float
    team_value: float

    def score(self, *, quality_weight: float = 1.0, cost_weight: float = 1.0, risk_weight: float = 1.0,
              learning_weight: float = 0.0, team_weight: float = 0.0) -> float:
        return (quality_weight * self.quality + learning_weight * self.learning + team_weight * self.team_value
                - cost_weight * self.cost - risk_weight * self.risk)


def allocate_by_utility(options: Mapping[str, Utility], *, budget: float | None = None) -> list[str]:
    if not options:
        return []
    if budget is not None and budget < 0:
        raise ValueError("budget_must_be_nonnegative")
    names = list(options)
    if budget is None:
        return sorted(names, key=lambda name: options[name].score(), reverse=True)
    if any(options[name].cost < 0 for name in names):
        raise ValueError("option_cost_must_be_nonnegative")
    # Exact 0/1 knapsack over integerized costs; unlike greedy ranking this
    # cannot discard a better feasible combination.
    scale = 1000
    capacity = int(round(budget * scale))
    costs = [int(round(options[name].cost * scale)) for name in names]
    dp = [(0.0, ()) for _ in range(capacity + 1)]
    for index, name in enumerate(names):
        cost = costs[index]
        value = options[name].score()
        for current in range(capacity, cost - 1, -1):
            candidate = (dp[current - cost][0] + value, dp[current - cost][1] + (index,))
            if candidate[0] > dp[current][0]:
                dp[current] = candidate
    selected_indices = set(dp[capacity][1])
    return [name for index, name in enumerate(names) if index in selected_indices]


def queue_metrics(arrival_rate: float, service_rate: float) -> dict[str, float | str]:
    if arrival_rate < 0 or service_rate <= 0:
        raise ValueError("invalid_queue_rates")
    utilization = arrival_rate / service_rate
    return {"arrival_rate": arrival_rate, "service_rate": service_rate, "utilization": utilization,
            "status": "unstable" if utilization >= 1 else "stable"}


@dataclass(frozen=True)
class BanditArm:
    successes: float = 1.0
    failures: float = 1.0

    def sample(self, rng: Random) -> float:
        if self.successes <= 0 or self.failures <= 0:
            raise ValueError("bandit_parameters_must_be_positive")
        return rng.betavariate(self.successes, self.failures)

    def observe(self, reward: float) -> "BanditArm":
        _unit(reward, "reward")
        return BanditArm(self.successes + reward, self.failures + 1 - reward)


class ThompsonBandit:
    def __init__(self, names: Sequence[str], seed: int | None = None):
        if not names or len(set(names)) != len(names):
            raise ValueError("bandit_names_must_be_unique")
        self.arms = {name: BanditArm() for name in names}
        self.rng = Random(seed)

    def choose(self) -> str:
        return max(self.arms, key=lambda name: self.arms[name].sample(self.rng))

    def observe(self, name: str, reward: float) -> None:
        if name not in self.arms:
            raise ValueError("bandit_arm_not_found")
        self.arms[name] = self.arms[name].observe(reward)
