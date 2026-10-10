from __future__ import annotations

from dataclasses import dataclass
from math import exp
from typing import Mapping


def _unit(value: float, name: str) -> float:
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name}_out_of_range")
    return value


def environment_drift(reference: Mapping[str, object], current: Mapping[str, object]) -> float:
    """Hamming drift over explicitly comparable environment keys; absent keys are unknown."""
    keys = set(reference) & set(current)
    if not keys:
        return 0.0
    return sum(reference[key] != current[key] for key in keys) / len(keys)


def multiplicative_freshness(*, age_days: float, half_life_days: float,
                             consecutive_failures: int = 0,
                             contradiction_count: int = 0,
                             environment_drift_score: float = 0.0,
                             dependency_drift_score: float = 0.0,
                             failure_rate: float = 0.35,
                             contradiction_rate: float = 0.55) -> float:
    """Independent attenuation factors are multiplied; correlated causes must be supplied once."""
    if age_days < 0 or half_life_days <= 0 or consecutive_failures < 0 or contradiction_count < 0:
        raise ValueError("invalid_degradation_parameters")
    _unit(environment_drift_score, "environment_drift_score")
    _unit(dependency_drift_score, "dependency_drift_score")
    _unit(failure_rate, "failure_rate")
    _unit(contradiction_rate, "contradiction_rate")
    time_factor = 2.0 ** (-age_days / half_life_days)
    failure_factor = exp(-failure_rate * consecutive_failures)
    contradiction_factor = exp(-contradiction_rate * contradiction_count)
    return time_factor * failure_factor * contradiction_factor * (1 - environment_drift_score) * (1 - dependency_drift_score)


def degradation_score(**kwargs: object) -> float:
    return max(0.0, min(1.0, 1.0 - multiplicative_freshness(**kwargs)))


def degradation_state(score: float, *, contradiction_count: int = 0, hard_failure: bool = False) -> str:
    _unit(score, "score")
    if hard_failure:
        return "invalidated"
    if contradiction_count > 0 or score >= 0.70:
        return "disputed"
    if score >= 0.45:
        return "degraded"
    if score >= 0.20:
        return "cooling"
    return "stable"


@dataclass(frozen=True)
class DegradationAssessment:
    score: float
    freshness: float
    state: str
    reasons: tuple[str, ...]
    model_version: str = "degradation-v1"


def assess_degradation(*, age_days: float, half_life_days: float, consecutive_failures: int = 0,
                       contradiction_count: int = 0, environment_drift_score: float = 0.0,
                       dependency_drift_score: float = 0.0, hard_failure: bool = False) -> DegradationAssessment:
    freshness = multiplicative_freshness(age_days=age_days, half_life_days=half_life_days,
        consecutive_failures=consecutive_failures, contradiction_count=contradiction_count,
        environment_drift_score=environment_drift_score, dependency_drift_score=dependency_drift_score)
    score = 1.0 if hard_failure else 1.0 - freshness
    reasons = []
    if age_days > 0: reasons.append("age")
    if consecutive_failures > 0: reasons.append("consecutive_failures")
    if contradiction_count > 0: reasons.append("contradiction")
    if environment_drift_score > 0: reasons.append("environment_drift")
    if dependency_drift_score > 0: reasons.append("dependency_drift")
    if hard_failure: reasons.append("hard_failure")
    return DegradationAssessment(score, freshness, degradation_state(score, contradiction_count=contradiction_count, hard_failure=hard_failure), tuple(reasons))
