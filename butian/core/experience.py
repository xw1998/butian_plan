from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
from math import exp, log1p
from typing import Iterable

class SkillPhase(StrEnum):
    ESTABLISHING = "establishing"
    DEVELOPING = "developing"
    STABLE = "stable"
    ACTIVATING = "activating"
    INTEGRATING = "integrating"
    TRANSCENDING = "transcending"

PHASE_ORDER = tuple(SkillPhase)

@dataclass(frozen=True)
class SkillState:
    phase: SkillPhase = SkillPhase.ESTABLISHING
    level: float = 0.0
    abstraction_level: int = 0
    marginal_gain: float = 0.0
    plateau_score: float = 0.0
    activation_count: int = 0

def saturated_strength(successes: int, base: float = 1.0, gain: float = 1.0) -> float:
    if successes < 0 or base < 0 or gain < 0:
        raise ValueError("strength_parameters_must_be_nonnegative")
    return base + gain * log1p(successes)

def phase_curve(progress: float, phase: SkillPhase) -> float:
    """Bounded within-phase growth; progress is normalized to [0, 1]."""
    if not 0 <= progress <= 1:
        raise ValueError("progress_out_of_range")
    steepness = {SkillPhase.ESTABLISHING: 7.0, SkillPhase.DEVELOPING: 5.0,
                 SkillPhase.ACTIVATING: 6.0, SkillPhase.INTEGRATING: 4.0,
                 SkillPhase.TRANSCENDING: 3.0, SkillPhase.STABLE: 1.0}[phase]
    value = (1 - exp(-steepness * progress)) / (1 - exp(-steepness))
    return value

def marginal_gain(gains: Iterable[float], window: int = 5) -> float:
    values = [float(x) for x in gains]
    if window <= 0: raise ValueError("window_must_be_positive")
    if len(values) < 2: return values[-1] if values else 0.0
    sample = values[-window:]
    return sum(sample) / len(sample)

def transition_phase(state: SkillState, *, marginal: float, contradiction: bool = False,
                     novel_environment: bool = False, independent_successes: int = 0,
                     composition_successes: int = 0, regression_evidence: bool = False) -> SkillState:
    """Explicit spiral transitions; no phase jump without observable evidence."""
    phase = state.phase
    next_phase = phase
    if regression_evidence and contradiction and phase in {SkillPhase.TRANSCENDING, SkillPhase.INTEGRATING}:
        next_phase = SkillPhase.ACTIVATING
    elif regression_evidence and contradiction and phase == SkillPhase.ACTIVATING:
        next_phase = SkillPhase.DEVELOPING
    elif phase == SkillPhase.ESTABLISHING and state.level >= 0.2:
        next_phase = SkillPhase.DEVELOPING
    elif phase == SkillPhase.DEVELOPING and marginal < 0.05 and state.level >= 0.5:
        next_phase = SkillPhase.STABLE
    elif phase == SkillPhase.STABLE and (contradiction or novel_environment) and state.level >= 0.5:
        next_phase = SkillPhase.ACTIVATING
    elif phase == SkillPhase.ACTIVATING and independent_successes >= 2:
        next_phase = SkillPhase.INTEGRATING
    elif phase == SkillPhase.INTEGRATING and composition_successes >= 2:
        next_phase = SkillPhase.TRANSCENDING
    activation_count = state.activation_count + int(phase == SkillPhase.STABLE and next_phase == SkillPhase.ACTIVATING)
    return SkillState(next_phase, min(1.0, max(0.0, state.level)), max(0, state.abstraction_level + int(next_phase == SkillPhase.TRANSCENDING) - int(phase == SkillPhase.TRANSCENDING and next_phase != phase)), max(0.0, marginal), min(1.0, max(0.0, 1 - marginal / 0.05)) if phase == SkillPhase.DEVELOPING else state.plateau_score, activation_count)

def composition_gain(component_gains: Iterable[float], *, composition_bonus: float = 0.0,
                    redundancy_penalty: float = 0.0, conflict_penalty: float = 0.0) -> float:
    """Components must be unique; bonuses are supplied only after observed synergy."""
    gains = [float(x) for x in component_gains]
    if any(x < 0 for x in gains) or min(composition_bonus, redundancy_penalty, conflict_penalty) < 0:
        raise ValueError("composition_values_must_be_nonnegative")
    return max(0.0, sum(gains) + composition_bonus - redundancy_penalty - conflict_penalty)

def decision_value(strength: float, environment_match: float, recall_quality: float,
                   composition_quality: float, evidence_confidence: float,
                   phase: SkillPhase, conflict_penalty: float = 0.0,
                   transfer_risk: float = 0.0) -> float:
    factors = (strength, environment_match, recall_quality, composition_quality, evidence_confidence)
    if any(x < 0 for x in factors) or any(x > 1 for x in factors[1:]) or min(conflict_penalty, transfer_risk) < 0:
        raise ValueError("decision_values_out_of_range")
    phase_factor = {SkillPhase.ESTABLISHING: .7, SkillPhase.DEVELOPING: 1., SkillPhase.STABLE: 1., SkillPhase.ACTIVATING: .8, SkillPhase.INTEGRATING: 1.2, SkillPhase.TRANSCENDING: 1.3}[phase]
    raw = strength * environment_match * recall_quality * composition_quality * evidence_confidence * phase_factor - conflict_penalty - transfer_risk
    return max(0.0, min(1.0, raw))
