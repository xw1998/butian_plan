from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.strategies import strategy_snapshot


@dataclass(frozen=True)
class StrategyRecommendation:
    strategy_id: str
    reason: str
    confidence: str


_PRIORITY = {
    "security": "risk_first",
    "privacy": "risk_first",
    "medical": "risk_first",
    "finance": "risk_first",
    "production": "risk_first",
    "science": "model_building",
    "complex_system": "model_building",
    "performance": "model_building",
    "research": "evidence_review",
    "fact_check": "evidence_review",
    "policy": "evidence_review",
    "debugging": "experimental_iteration",
    "unknown": "experimental_iteration",
    "prototype": "experimental_iteration",
    "design": "design_synthesis",
    "architecture": "design_synthesis",
    "requirements": "user_context",
    "support": "user_context",
    "review": "adversarial_audit",
    "release": "adversarial_audit",
}


def recommend(task_features: dict[str, Any]) -> list[StrategyRecommendation]:
    types = set(task_features.get("task_types", []))
    risk = task_features.get("risk", "normal")
    recommendations: list[StrategyRecommendation] = []
    if risk in {"high", "critical"}:
        recommendations.append(StrategyRecommendation("risk_first", "风险等级要求先完成权限、失效模式和回退检查", "high"))
    for task_type in task_features.get("task_types", []):
        strategy_id = _PRIORITY.get(task_type)
        if strategy_id and strategy_id not in {item.strategy_id for item in recommendations}:
            recommendations.append(StrategyRecommendation(strategy_id, f"任务类型 {task_type} 与该策略的适用范围匹配", "medium"))
    if not recommendations:
        recommendations.append(StrategyRecommendation("experience_reuse", "任务特征不足，使用可复用经历并先做环境核对", "low"))
    if "review" in types or "critical_decision" in types:
        if all(item.strategy_id != "adversarial_audit" for item in recommendations):
            recommendations.append(StrategyRecommendation("adversarial_audit", "重要决策需要独立反方检查", "medium"))
    return recommendations


def compose(member_strategies: list[dict[str, Any]], task_features: dict[str, Any]) -> dict[str, Any]:
    recommendations = recommend(task_features)
    available = {item["strategy_id"]: item for item in member_strategies}
    selected = []
    for recommendation in recommendations:
        member = available.get(recommendation.strategy_id)
        if member:
            selected.append({"member_id": member["member_id"], "strategy": member, "reason": recommendation.reason, "confidence": recommendation.confidence})
    if not selected and member_strategies:
        selected.append({"member_id": member_strategies[0]["member_id"], "strategy": member_strategies[0], "reason": "没有足够依据匹配专门策略，保留成员默认策略", "confidence": "low"})
    return {"task_features": task_features, "recommendations": [item.__dict__ for item in recommendations], "assignments": selected}
