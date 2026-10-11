import pytest

from butian.core.strategies import strategy_snapshot, validate_strategy_order
from butian.runtime.service import ButianService
from butian.mcp_server import call_tool


def test_strategy_order_and_guard_validation():
    snapshot = strategy_snapshot("model_building")
    assert snapshot["stages"][:2] == ["define_system_boundary", "build_model"]
    with pytest.raises(ValueError, match="unknown_strategy_stage"):
        validate_strategy_order("model_building", ["invent_stage"])


def test_member_strategy_and_composition(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("eng", "工程师", default_strategy_id="experience_reuse")
    service.add_member("sci", "科学家", default_strategy_id="model_building")
    composed = service.compose_strategies(["eng", "sci"], {"task_types": ["science"], "risk": "normal"})
    assert composed["assignments"][0]["member_id"] == "sci"
    service.close()


def test_strategy_override_is_auditable(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("d", "派活者")
    session = service.create_session("team", "d")["session_id"]
    task = service.store.create_task(session, "发布审核")
    snapshot = service.assign_strategy(task, "adversarial_audit", reason="发布任务需要反方检查", decision_by="d")
    assert snapshot["override"] is True
    stored = service.store.list_tasks()[0]["strategy_snapshot"]
    assert "发布任务需要反方检查" in stored
    service.close()


def test_mcp_strategy_recommendation(tmp_path):
    service = ButianService(tmp_path)
    with pytest.raises(ValueError, match="admin_authorization_required"):
        call_tool(service, "butian_strategy_recommend", {"task_features": {"task_types": ["security"], "risk": "critical"}})
    result = {"recommendations": service.recommend_strategies({"task_types": ["security"], "risk": "critical"})}
    assert result["recommendations"][0]["strategy_id"] == "risk_first"
    service.close()


def test_strategy_steps_reject_unknown_stage_and_record_actual_step(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("d", "派活者")
    session = service.create_session("team", "d")["session_id"]
    task = service.store.create_task(session, "模型任务")
    with pytest.raises(ValueError, match="unknown_strategy_stage"):
        service.store.record_strategy_step(task, 1, "model_building", "act")
    step_id = service.store.record_strategy_step(task, 1, "model_building", "build_model", member_id="d", output_ref="event:x")
    assert service.store.list_strategy_steps(task)[0]["step_id"] == step_id
    service.close()
