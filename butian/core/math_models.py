from __future__ import annotations

from dataclasses import dataclass
from math import exp, isfinite, lgamma, log, log1p
from random import Random
from typing import Iterable, Mapping, Sequence


def _unit(value: float, name: str) -> float:
    value = float(value)
    if not isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name}_out_of_range")
    return value


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    tiny = 1e-300
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 201):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-14:
            return h
    raise ArithmeticError("beta_cdf_failed_to_converge")


def _regularized_beta(x: float, a: float, b: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = exp(lgamma(a + b) - lgamma(a) - lgamma(b) + a * log(x) + b * log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_continued_fraction(a, b, x) / a
    return 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b


def _beta_quantile(probability: float, a: float, b: float) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(90):
        mid = (lo + hi) / 2.0
        if _regularized_beta(mid, a, b) < probability:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


@dataclass(frozen=True)
class BetaBelief:
    alpha: float = 1.0
    beta: float = 1.0
    observations: float = 0.0

    def __post_init__(self) -> None:
        if not all(isfinite(value) for value in (self.alpha, self.beta, self.observations)) or self.alpha <= 0 or self.beta <= 0 or self.observations < 0:
            raise ValueError("beta_parameters_must_be_positive")

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def effective_samples(self) -> float:
        return self.observations

    @property
    def prior_strength(self) -> float:
        return self.alpha + self.beta - self.observations

    def update(self, success_weight: float = 0.0, failure_weight: float = 0.0) -> "BetaBelief":
        if success_weight < 0 or failure_weight < 0:
            raise ValueError("evidence_weights_must_be_nonnegative")
        if success_weight == 0 and failure_weight == 0:
            raise ValueError("evidence_weight_required")
        return BetaBelief(self.alpha + success_weight, self.beta + failure_weight, self.observations + success_weight + failure_weight)

    def interval(self, confidence: float = 0.95) -> tuple[float, float]:
        _unit(confidence, "confidence")
        if confidence <= 0:
            raise ValueError("confidence_must_be_positive")
        tail = (1.0 - confidence) / 2.0
        return (_beta_quantile(tail, self.alpha, self.beta),
                _beta_quantile(1.0 - tail, self.alpha, self.beta))


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
    values = [float(value) for value in candidates.values()]
    if not candidates or any(not isfinite(value) or value < 0 for value in values):
        raise ValueError("invalid_transition_scores")
    total = sum(values)
    if not isfinite(total):
        raise ValueError("invalid_transition_scores")
    if total <= 0:
        raise ValueError("transition_scores_have_zero_mass")
    return {state: value / total for state, value in zip(candidates, values)}


def entropy(probabilities: Iterable[float]) -> float:
    values = [_unit(value, "probability") for value in probabilities]
    total = sum(values)
    if not values or abs(total - 1.0) > 1e-9:
        raise ValueError("probabilities_must_sum_to_one")
    return -sum(value * log(value, 2) for value in values if value > 0)


def information_gain(prior: Sequence[float], conditional: Sequence[float]) -> float:
    """Information reduction for one explicitly supplied posterior."""
    prior_entropy, posterior_entropy = entropy(prior), entropy(conditional)
    if posterior_entropy > prior_entropy + 1e-9:
        raise ValueError("posterior_entropy_exceeds_prior")
    return max(0.0, prior_entropy - posterior_entropy)


def expected_information_gain(prior: Sequence[float], posteriors: Sequence[Sequence[float]], outcome_probabilities: Sequence[float]) -> float:
    """Expected entropy reduction across mutually exclusive observations.

    The caller must provide a normalized prior, outcome probabilities, and one
    normalized posterior per outcome. No outcome or posterior is inferred.
    """
    if len(posteriors) != len(outcome_probabilities) or not posteriors:
        raise ValueError("posterior_outcome_length_mismatch")
    raw_probabilities = [float(value) for value in outcome_probabilities]
    if any(not isfinite(value) or value < 0 or value > 1 for value in raw_probabilities):
        raise ValueError("outcome_probability_out_of_range")
    if abs(sum(raw_probabilities) - 1.0) > 1e-9:
        raise ValueError("outcome_probabilities_must_sum_to_one")
    probabilities = raw_probabilities
    prior_entropy = entropy(prior)
    if any(len(posterior) != len(prior) for posterior in posteriors):
        raise ValueError("posterior_state_length_mismatch")
    posterior_entropies = [entropy(posterior) for posterior in posteriors]
    for index, (probability, posterior) in enumerate(zip(probabilities, posteriors)):
        for state_probability, posterior_probability in zip(prior, posterior):
            if probability * posterior_probability > state_probability + 1e-9:
                raise ValueError(f"posterior_violates_bayes_consistency:{index}")
    for state, prior_probability in enumerate(prior):
        mixture = sum(probability * posterior[state]
                      for probability, posterior in zip(probabilities, posteriors))
        if abs(mixture - prior_probability) > 1e-9:
            raise ValueError("posterior_mixture_must_equal_prior")
    expected_posterior_entropy = sum(probability * value for probability, value in zip(probabilities, posterior_entropies))
    if expected_posterior_entropy > prior_entropy + 1e-9:
        raise ValueError("expected_posterior_entropy_exceeds_prior")
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
    if any(not isfinite(value) or value < 0 or value > 1 for value in (*treated, *control)):
        raise ValueError("outcome_out_of_range")
    t, c = sum(treated) / len(treated), sum(control) / len(control)
    return CausalEstimate(t, c, t - c, len(treated), len(control))


def stratified_effect(strata: Mapping[str, tuple[Sequence[float], Sequence[float]]], *, weights: Mapping[str, float]) -> CausalEstimate:
    """Standardize to an explicitly declared target population across strata."""
    if not strata or set(weights) != set(strata):
        raise ValueError("strata_and_weights_must_match")
    normalized = {name: float(weight) for name, weight in weights.items()}
    if any(not isfinite(value) or value < 0 for value in normalized.values()) or abs(sum(normalized.values()) - 1.0) > 1e-9:
        raise ValueError("stratum_weights_must_sum_to_one")
    if any(not treated or not control for treated, control in strata.values()):
        raise ValueError("empty_stratum_group")
    estimates = {name: difference_in_means(treated, control) for name, (treated, control) in strata.items()}
    treated_mean = sum(normalized[name] * item.treated_mean for name, item in estimates.items())
    control_mean = sum(normalized[name] * item.control_mean for name, item in estimates.items())
    return CausalEstimate(treated_mean, control_mean, treated_mean - control_mean,
                          sum(item.treated_count for item in estimates.values()),
                          sum(item.control_count for item in estimates.values()))


@dataclass(frozen=True)
class Utility:
    quality: float
    cost: float
    risk: float
    learning: float
    team_value: float

    def __post_init__(self) -> None:
        if not all(isfinite(value) for value in (self.quality, self.cost, self.risk, self.learning, self.team_value)):
            raise ValueError("utility_values_must_be_finite")
        if self.cost < 0 or self.risk < 0:
            raise ValueError("utility_cost_and_risk_must_be_nonnegative")

    def score(self, *, quality_weight: float = 1.0, cost_weight: float = 1.0, risk_weight: float = 1.0,
              learning_weight: float = 0.0, team_weight: float = 0.0) -> float:
        weights = (quality_weight, cost_weight, risk_weight, learning_weight, team_weight)
        if not all(isfinite(value) for value in weights) or any(value < 0 for value in weights):
            raise ValueError("utility_weights_must_be_nonnegative")
        return (quality_weight * self.quality + learning_weight * self.learning + team_weight * self.team_value
                - cost_weight * self.cost - risk_weight * self.risk)


def allocate_by_utility(options: Mapping[str, Utility], *, budget: float | None = None) -> list[str]:
    if not options:
        return []
    if budget is not None and (not isfinite(budget) or budget < 0):
        raise ValueError("budget_must_be_nonnegative")
    names = list(options)
    if budget is None:
        return [name for name in sorted(names, key=lambda name: options[name].score(), reverse=True)
                if options[name].score() > 0]
    if any(not isfinite(options[name].cost) or options[name].cost < 0 for name in names):
        raise ValueError("option_cost_must_be_nonnegative")
    # Sparse Pareto frontier avoids allocating memory proportional to budget.
    frontier: list[tuple[float, float, tuple[int, ...]]] = [(0.0, 0.0, ())]
    max_states = 100_000
    for index, name in enumerate(names):
        option = options[name]
        value = option.score()
        if value <= 0 or option.cost > budget:
            continue
        candidates = frontier + [(cost + option.cost, score + value, chosen + (index,))
                                 for cost, score, chosen in frontier if cost + option.cost <= budget]
        candidates.sort(key=lambda state: (state[0], -state[1]))
        frontier = []
        best_score = float("-inf")
        for state in candidates:
            if state[1] > best_score:
                frontier.append(state)
                best_score = state[1]
        if len(frontier) > max_states:
            raise ValueError("knapsack_frontier_limit_exceeded")
    selected = max(frontier, key=lambda state: state[1])[2]
    selected_indices = set(selected)
    return [name for index, name in enumerate(names) if index in selected_indices]


def queue_metrics(arrival_rate: float, service_rate: float) -> dict[str, float | str]:
    if not isfinite(arrival_rate) or not isfinite(service_rate) or arrival_rate < 0 or service_rate <= 0:
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
        if reward not in (0.0, 1.0):
            raise ValueError("bernoulli_bandit_requires_binary_reward")
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
