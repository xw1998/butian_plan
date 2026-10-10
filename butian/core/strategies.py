from __future__ import annotations

from dataclasses import dataclass
from typing import Any


COMMON_GUARDS = (
    "define_goal_and_acceptance",
    "separate_facts_hypotheses_evidence",
    "prefer_reversible_high_information_actions",
    "require_authorization_for_irreversible_actions",
    "record_prediction_observation_and_stop_reason",
)


@dataclass(frozen=True)
class Strategy:
    strategy_id: str
    version: int
    name: str
    stages: tuple[str, ...]
    task_types: tuple[str, ...]
    avoid_when: tuple[str, ...] = ()
    guards: tuple[str, ...] = COMMON_GUARDS


_BUILTINS = (
    Strategy("experience_reuse", 1, "经验复用型", ("recall_experiences", "compare_environment", "check_authority", "design_small_test", "act", "retrospect"), ("engineering", "known_issue", "toolchain")),
    Strategy("model_building", 1, "模型建构型", ("define_system_boundary", "build_model", "derive_predictions", "choose_discriminating_observation", "update_model"), ("science", "complex_system", "performance", "inference")),
    Strategy("evidence_review", 1, "证据审查型", ("split_claims", "check_sources", "search_counterexamples", "bound_applicability", "report_confidence"), ("research", "fact_check", "policy", "high_consequence")),
    Strategy("experimental_iteration", 1, "实验迭代型", ("list_hypotheses", "choose_information_gain_test", "control_variables", "observe", "update_or_stop"), ("unknown", "debugging", "prototype")),
    Strategy("design_synthesis", 1, "设计综合型", ("clarify_stakeholders", "generate_options", "compare_constraints", "prototype_or_simulate", "converge_with_feedback"), ("design", "architecture", "product")),
    Strategy("risk_first", 1, "风险先行型", ("identify_harm_and_authority", "list_failure_modes", "build_guardrails_and_rollback", "safe_validation", "authorized_execution"), ("security", "privacy", "medical", "finance", "production")),
    Strategy("user_context", 1, "用户情境型", ("clarify_user_goal", "ask_minimum_questions", "calibrate_acceptance", "propose_solution", "check_usability"), ("requirements", "support", "collaboration")),
    Strategy("adversarial_audit", 1, "反方审计型", ("find_likely_failure", "construct_counterexample", "inspect_assumptions_and_costs", "recommend_fix_or_reject"), ("review", "release", "critical_decision")),
)


STRATEGIES = {item.strategy_id: item for item in _BUILTINS}


def get_strategy(strategy_id: str, version: int | None = None) -> Strategy:
    strategy = STRATEGIES.get(strategy_id)
    if strategy is None or (version is not None and strategy.version != version):
        raise ValueError("strategy_not_found")
    return strategy


def validate_strategy_order(strategy_id: str, stages: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    strategy = get_strategy(strategy_id)
    expected = tuple(strategy.stages)
    actual = tuple(stages)
    if not actual or len(set(actual)) != len(actual):
        raise ValueError("invalid_strategy_order")
    unknown = set(actual) - set(expected)
    if unknown:
        raise ValueError("unknown_strategy_stage:" + ",".join(sorted(unknown)))
    if not set(strategy.guards).issubset(set(COMMON_GUARDS)):
        raise ValueError("invalid_strategy_guards")
    return actual


def strategy_snapshot(strategy_id: str, stages: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    strategy = get_strategy(strategy_id)
    selected = validate_strategy_order(strategy_id, stages or strategy.stages)
    return {"strategy_id": strategy.strategy_id, "version": strategy.version, "name": strategy.name, "stages": list(selected), "task_types": list(strategy.task_types), "avoid_when": list(strategy.avoid_when), "guards": list(strategy.guards)}
