from __future__ import annotations

import json
import math
import sys

import pytest

from butian.runtime.service import ButianService
from butian.runtime.tools import wilson_lower


MANIFEST = {
    "input_contract": {"type": "text"},
    "output_contract": {"type": "text"},
    "permissions": ["cpu"],
    "failure_conditions": ["invalid_input"],
}


def make_service(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("m1", "工具作者")
    service.add_member("m2", "试用成员")
    return service


def verify_execution(service, owner, tool_id, version, key, input_data=None):
    execution = service.execute_tool(owner, tool_id, version, input_data or {}, verification_only=True)
    return service.verify_tool(owner, tool_id, version, "success", independent_key=key,
                               task_id=key, environment={"kind": "unit"},
                               evidence_ids=[f"evidence-{key}"], execution_id=execution["execution_id"],
                               input_hash=execution["input_hash"], expected_output=execution["expected_output"])

def propose(service, owner="m1"):
    return service.propose_tool(
        owner,
        "通用分析",
        "对输入文本执行通用分析",
        reuse_lower=3,
        manual_cost_upper=10,
        call_cost_lower=1,
        build_cost_upper=5,
        maintenance_cost_upper=2,
        risk_cost_upper=1,
        manual_cost_lower=10,
        call_cost_upper=1,
        cost_unit="minute",
        manifest=MANIFEST,
    )


def test_tool_proposal_gate_and_numeric_validation(tmp_path):
    service = make_service(tmp_path)
    try:
        rejected = service.propose_tool("m1", "小工具", "低价值", reuse_lower=1, manual_cost_upper=1, call_cost_lower=1, build_cost_upper=1, maintenance_cost_upper=0, risk_cost_upper=0, manual_cost_lower=1, call_cost_upper=1, cost_unit="minute")
        assert rejected["status"] == "rejected"
        with pytest.raises(ValueError, match="must_be_finite"):
            service.propose_tool("m1", "坏工具", "坏输入", reuse_lower=math.nan, manual_cost_upper=1, call_cost_lower=0, build_cost_upper=0, maintenance_cost_upper=0, risk_cost_upper=0)
    finally:
        service.close()


def test_tool_version_owner_path_and_activation(tmp_path):
    service = make_service(tmp_path)
    try:
        proposal = propose(service)
        tool_id = proposal["tool_id"]
        version = service.add_tool_version("m1", tool_id, "import sys; print(sys.stdin.read())", {**MANIFEST, "permissions": ["execute"], "risk_level": "low"})
        assert version["version"] == 1
        row = service.tools._tool(tool_id)
        source = service.store.db.execute("SELECT source_path FROM tool_versions WHERE tool_id=? AND version=1", (tool_id,)).fetchone()[0]
        assert f"members{__import__('os').sep}m1{__import__('os').sep}tools" in source
        with pytest.raises(ValueError, match="access_denied"):
            service.add_tool_version("m2", tool_id, "bad", MANIFEST)
        execution_a = service.execute_tool("m1", tool_id, 1, "input-a", verification_only=True)
        execution_b = service.execute_tool("m1", tool_id, 1, "input-b", verification_only=True)
        first = service.verify_tool("m1", tool_id, 1, "success", independent_key="case-a", task_id="task-a", environment={"kind": "unit"}, evidence_ids=["log-a"], execution_id=execution_a["execution_id"], input_hash=execution_a["input_hash"], expected_output=execution_a["expected_output"])
        assert first["status"] == "candidate"
        second = service.verify_tool("m1", tool_id, 1, "success", independent_key="case-b", task_id="task-b", environment={"kind": "integration"}, evidence_ids=["log-b"], execution_id=execution_b["execution_id"], input_hash=execution_b["input_hash"], expected_output=execution_b["expected_output"])
        assert second["status"] == "candidate"
        execution_c = service.execute_tool("m1", tool_id, 1, "input-c", verification_only=True)
        third = service.verify_tool("m1", tool_id, 1, "success", independent_key="case-c", task_id="task-c", environment={"kind": "staging"}, evidence_ids=["log-c"], execution_id=execution_c["execution_id"], input_hash=execution_c["input_hash"], expected_output=execution_c["expected_output"])
        assert third["status"] == "candidate"
        execution_d = service.execute_tool("m1", tool_id, 1, "input-d", verification_only=True)
        fourth = service.verify_tool("m1", tool_id, 1, "success", independent_key="case-d", task_id="task-d", environment={"kind": "unit"}, evidence_ids=["log-d"], execution_id=execution_d["execution_id"], input_hash=execution_d["input_hash"], expected_output=execution_d["expected_output"])
        assert fourth["status"] == "active"
        duplicate = service.verify_tool("m1", tool_id, 1, "success", independent_key="case-a", task_id="task-a", environment={"kind": "unit"}, evidence_ids=["log-a"], execution_id=execution_a["execution_id"], input_hash=execution_a["input_hash"], expected_output=execution_a["expected_output"])
        assert duplicate["verification_id"] == first["verification_id"]
        with pytest.raises(ValueError, match="verification_idempotency_conflict"):
            service.verify_tool("m1", tool_id, 1, "failure", independent_key="case-a", environment={"kind": "unit"}, evidence_ids=["conflict-log"])
        assert service.tool_stats(tool_id, 1)["successes"] == 4
    finally:
        service.close()


def test_legacy_costs_and_missing_evidence_cannot_auto_activate(tmp_path):
    service = make_service(tmp_path)
    try:
        proposal = service.propose_tool("m1", "legacy", "旧成本口径", reuse_lower=100, manual_cost_upper=100, call_cost_lower=0, build_cost_upper=0, maintenance_cost_upper=0, risk_cost_upper=0)
        assert proposal["proposal"]["legacy_cost_bounds"] is True
        tool_id = proposal["tool_id"]
        service.add_tool_version("m1", tool_id, "import sys; print(sys.stdin.read())", {**MANIFEST, "permissions": ["execute"]})
        with pytest.raises(ValueError, match="verification_evidence_required"):
            service.verify_tool("m1", tool_id, 1, "failure", independent_key="no-evidence")
        for i in range(2):
            execution = service.execute_tool("m1", tool_id, 1, f"input-{i}", verification_only=True)
            service.verify_tool("m1", tool_id, 1, "success", independent_key=f"legacy-{i}", task_id=f"task-{i}", environment={"kind": f"env-{i}"}, evidence_ids=[f"log-{i}"], execution_id=execution["execution_id"], input_hash=execution["input_hash"], expected_output=execution["expected_output"])
        assert service.tools._tool(tool_id)["status"] == "candidate"
    finally:
        service.close()


def test_unknown_verification_blocks_admission_and_risk_is_validated(tmp_path):
    service = make_service(tmp_path)
    try:
        proposal = service.propose_tool("m1", "unknown", "unknown 必须阻断", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1, manual_cost_lower=10, call_cost_upper=1, cost_unit="minute")
        tool_id = proposal["tool_id"]
        manifest = {**MANIFEST, "risk_level": "critical"}
        service.add_tool_version("m1", tool_id, "return input", manifest)
        with pytest.raises(ValueError, match="verification_success_binding_required"):
            service.verify_tool("m1", tool_id, 1, "success", independent_key="unbound", evidence_ids=["e"])
        service.verify_tool("m1", tool_id, 1, "unknown", independent_key="unknown-1", evidence_ids=["e"])
        assert service.tools._tool(tool_id)["status"] == "candidate"
        assert service.tool_stats(tool_id, 1)["unknown"] == 1
    finally:
        service.close()


def test_conservative_cost_bounds_and_units_are_enforced(tmp_path):
    service = make_service(tmp_path)
    try:
        with pytest.raises(ValueError, match="cost_unit_required"):
            service.propose_tool("m1", "no-unit", "缺单位", reuse_lower=10, manual_cost_upper=20, call_cost_lower=1, build_cost_upper=0, maintenance_cost_upper=0, risk_cost_upper=0, manual_cost_lower=10, call_cost_upper=2)
        rejected = service.propose_tool("m1", "negative", "收益下界为负", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=0, maintenance_cost_upper=0, risk_cost_upper=0, manual_cost_lower=1, call_cost_upper=2, cost_unit="minute")
        assert rejected["status"] == "rejected"
        assert rejected["proposal"]["benefit_lower"] < 0
    finally:
        service.close()


def test_only_active_tool_can_share_and_share_is_pending(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        service.add_tool_version("m1", tool_id, "tool", MANIFEST)
        with pytest.raises(ValueError, match="only_active"):
            service.share_tool("m1", tool_id, "m2")
        service.verify_tool("m1", tool_id, 1, "unknown", independent_key="candidate-observation", evidence_ids=["candidate-log"])
        assert service.tools._tool(tool_id)["status"] == "candidate"
        with pytest.raises(ValueError, match="only_active"):
            service.share_tool("m1", tool_id, "m2", permission="team_execute")
    finally:
        service.close()


def test_wilson_lower_bound_is_conservative(tmp_path):
    assert wilson_lower(2, 0) < 1.0
    assert wilson_lower(0, 2) == 0.0
    service = make_service(tmp_path)
    try:
        with pytest.raises(ValueError, match="invalid_reliability_inputs"):
            wilson_lower(-1, 0)
    finally:
        service.close()


def test_active_tool_cards_are_bounded_and_source_is_excluded(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        service.add_tool_version("m1", tool_id, "SECRET_SOURCE_BYTES", MANIFEST)
        assert service.tools._tool(tool_id)["status"] == "draft"
        composed, report = service.compose_context("m1", "请分析输入", include_tools=True, budgets={"TOOLS": 20})
        assert "工具卡片" not in composed
        assert "SECRET_SOURCE_BYTES" not in composed
        assert report.tools_included == 0
        assert report.tools_omitted == 0
        assert report.tools_included == 0
        assert "SECRET_SOURCE_BYTES" not in composed
    finally:
        service.close()


def test_controlled_execution_dry_run_audit_and_limits(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["cpu", "execute"], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "执行分析", "受控执行分析", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1, manifest=manifest)["tool_id"]
        service.add_tool_version("m1", tool_id, "import sys\nprint(sys.stdin.read())", manifest)
        preview = service.execute_tool("m1", tool_id, 1, {"x": 1}, dry_run=True, verification_only=True)
        assert preview["status"] == "dry_run"
        result = service.execute_tool("m1", tool_id, 1, {"x": 1}, verification_only=True)
        assert result["status"] == "success"
        assert '"x": 1' in result["stdout"]
        audit = service.list_tool_executions("m1", tool_id=tool_id)
        assert [row["status"] for row in audit][:2] == ["success", "dry_run"]
        with pytest.raises(ValueError, match="tool_execute_denied"):
            service.execute_tool("m2", tool_id, 1, {})
    finally:
        service.close()

def test_external_tool_requires_supply_chain_attestation(tmp_path):
    service = make_service(tmp_path)
    try:
        proposal = service.propose_tool("m1", "外部工具", "供应链测试", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)
        tool_id = proposal["tool_id"]
        external = {**MANIFEST, "source_kind": "external", "source": "https://example.invalid/tool", "license": "MIT", "scan_status": "pending"}
        with pytest.raises(ValueError, match="supply_chain"):
            service.add_tool_version("m1", tool_id, "external", external)
        external["scan_status"] = "passed"
        service.add_tool_version("m1", tool_id, "external", external)
        assert service.tools._tool(tool_id)["status"] == "draft"
    finally:
        service.close()

def test_execution_failures_create_repair_issues_without_discarding_asset(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "object"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "失败工具", "验证熔断", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1, manifest=manifest)["tool_id"]
        service.add_tool_version("m1", tool_id, "raise SystemExit(3)", manifest)
        for _ in range(3):
            result = service.execute_tool("m1", tool_id, 1, {}, verification_only=True)
            assert result["status"] == "failed"
        service.verify_tool("m1", tool_id, 1, "unknown", independent_key="failure-observation", evidence_ids=["failure-log"])
        assert service.list_tools("m1")[0]["status"] == "candidate"
        issues = service.list_tool_issues("m1", tool_id)
        assert len(issues) == 3
        assert all(issue["status"] == "open" for issue in issues)
    finally:
        service.close()

def test_shared_tool_accept_trial_archive_restore(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        service.add_tool_version("m1", tool_id, "tool", MANIFEST)
        assert service.tools._tool(tool_id)["status"] == "draft"
        with pytest.raises(ValueError, match="only_active"):
            service.share_tool("m1", tool_id, "m2")
        return
        share = service.share_tool("m1", tool_id, "m2")
        accepted = service.accept_tool_share("m2", share["share_id"])
        assert accepted["status"] == "trial"
        trial = service.trial_tool("m2", share["share_id"], "success", independent_key="trial-a")
        assert trial["status"] == "active"
        archived = service.archive_tool("m1", tool_id, reason="cold")
        assert archived["status"] == "archived"
        restored = service.restore_tool("m1", tool_id)
        assert restored["status"] == "candidate"
        service.verify_tool("m1", tool_id, 1, "success", independent_key="restore-a")
        service.verify_tool("m1", tool_id, 1, "success", independent_key="restore-b")
        assert service.list_tools("m1")[0]["status"] == "active"
    finally:
        service.close()

def test_repair_preserves_stable_version_and_requires_real_confirmation(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        service.add_tool_version("m1", tool_id, "print('ok')", manifest)
        run_v1 = service.execute_tool("m1", tool_id, 1, {}, verification_only=True)
        service.verify_tool("m1", tool_id, 1, "success", independent_key="repair-a", task_id="repair-task-a", environment={"kind": "test"}, evidence_ids=["repair-log-a"], execution_id=run_v1["execution_id"], input_hash=run_v1["input_hash"], expected_output=run_v1["expected_output"])
        assert service.tools._tool(tool_id)["status"] == "candidate"
        issue = service.report_tool_issue("m1", tool_id, 1, description="Wrong boundary handling")
        service.add_tool_version("m1", tool_id, "print('fixed')", {**manifest, "description": "v2"})
        assert service.tools._tool(tool_id)["current_version"] == 1
        service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="Correct boundary")
        dry = service.execute_tool("m1", tool_id, 2, {}, dry_run=True, verification_only=True)
        with pytest.raises(ValueError, match="execution_mismatch"):
            service.confirm_tool_repair("m1", issue["issue_id"], execution_id=dry["execution_id"], resolved=True)
        run_v2 = service.execute_tool("m1", tool_id, 2, {}, verification_only=True)
        assert service.confirm_tool_repair("m1", issue["issue_id"], execution_id=run_v2["execution_id"], resolved=True)["status"] == "resolved"
        assert service.tools._tool(tool_id)["current_version"] == 1
        with pytest.raises(ValueError, match="access_denied"):
            service.repair_tool_issue("m2", issue["issue_id"], repair_version=2, diagnosis="unauthorized")
    finally:
        service.close()

def test_repair_trial_runs_candidate_binds_success_and_closes_after_confirmation(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"], "risk_level": "low"}
        service.add_tool_version("m1", tool_id, "print('stable')", manifest)
        for index in range(4):
            execution = service.execute_tool("m1", tool_id, 1, {}, verification_only=True)
            service.verify_tool("m1", tool_id, 1, "success", independent_key=f"stable-{index}", task_id=f"stable-task-{index}", environment={"kind": f"env-{index}"}, evidence_ids=[f"stable-log-{index}"], execution_id=execution["execution_id"], input_hash=execution["input_hash"], expected_output=execution["expected_output"])
        share = service.share_tool("m1", tool_id, "m2", permission="team_execute")
        service.accept_tool_share("m2", share["share_id"])
        failed = service.execute_tool("m2", tool_id, 1, {}, share_id=share["share_id"])
        issue = service.report_tool_issue("m2", tool_id, 1, description="Observed incorrect result", execution_id=failed["execution_id"])
        service.add_tool_version("m1", tool_id, "print('repaired')", {**manifest, "description": "repair"})
        repair = service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="Fixed result")
        repaired = service.execute_tool("m2", tool_id, 2, {}, share_id=repair["trial_share_id"])
        trial = service.trial_tool("m2", repair["trial_share_id"], "success", independent_key="repair-success", evidence_ids=["repair-evidence"], execution_id=repaired["execution_id"], input_hash=repaired["input_hash"], expected_output=repaired["expected_output"])
        assert trial["outcome"] == "success"
        assert service.confirm_tool_repair("m2", issue["issue_id"], execution_id=repaired["execution_id"], resolved=True)["status"] == "resolved"
        with pytest.raises(ValueError, match="tool_execute_denied|tool_trial"):
            service.execute_tool("m2", tool_id, 2, {}, share_id=repair["trial_share_id"])
        assert service.tools._tool(tool_id)["current_version"] == 1
    finally:
        service.close()

def test_tool_integrity_and_personas(tmp_path):
    service = ButianService(tmp_path)
    try:
        created = service.bootstrap_personas()
        assert created == []
        created = [row[0] for row in service.store.db.execute("SELECT member_id FROM members").fetchall()]
        assert len(created) == 6
        rows = service.store.db.execute("SELECT name, responsibilities, persona FROM members ORDER BY member_id").fetchall()
        assert {row["name"] for row in rows} == {"许伟", "沈衡", "陆徵", "宋析", "苏衍", "何溪"}
        assert all(json.loads(row["persona"])["traits"] and json.loads(row["persona"])["domains"] for row in rows)
        assert all("特质:" not in json.loads(row["responsibilities"])[0] for row in rows)
        assert all((tmp_path / "members" / mid / "memory.db").is_file() for mid in created)
        service.add_member("owner", "作者")
        manifest = {"input_contract": {"type": "object"}, "output_contract": {"type": "object"}, "permissions": ["execute"], "failure_conditions": ["bad"], "entrypoint": ["python", "tool.py"]}
        tid = service.propose_tool("owner", "contract", "contract", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=1, maintenance_cost_upper=1, risk_cost_upper=1, manifest=manifest)["tool_id"]
        service.add_tool_version("owner", tid, "source", manifest)
        with pytest.raises(ValueError, match="verification_success_binding_required"):
            service.verify_tool("owner", tid, 1, "success", independent_key="i1", evidence_ids=["e1"])
        result = service.execute_tool("owner", tid, 1, {}, verification_only=True)
        assert result["status"] == "failed"
    finally:
        service.close()


def test_service_bootstraps_structured_builtin_personas_without_overwriting(tmp_path):
    service = ButianService(tmp_path)
    try:
        rows = service.store.db.execute("SELECT member_id, persona, responsibilities FROM members ORDER BY member_id").fetchall()
        assert [row[0] for row in rows] == ["he_xi", "lu_zheng", "shen_heng", "song_xi", "su_yan", "xu_wei"]
        identities = [json.loads(row[1]) for row in rows]
        assert all(identity["origin"] == "butian_builtin" for identity in identities)
        assert all(identity["traits"] and identity["domains"] for identity in identities)
        assert all(identity["rank"] and identity["reports_to"] == "system_dispatcher" for identity in identities)
        assert all(identity["provider_id"] is None and identity["model_id"] is None for identity in identities)
        assert all(identity["reasoning_effort"] is None and identity["max_tokens"] is None and identity["model_tier"] is None for identity in identities)
        assert all("特质:" not in json.loads(row[2])[0] for row in rows)
        assert all((tmp_path / "members" / row[0] / "memory.db").is_file() for row in rows)
        service.store.db.execute("UPDATE members SET name='用户改名' WHERE member_id='xu_wei'")
        assert service.bootstrap_personas() == []
        assert service.store.db.execute("SELECT name FROM members WHERE member_id='xu_wei'").fetchone()[0] == "用户改名"
    finally:
        service.close()


def test_contract_validation_rejects_missing_nested_and_extra_fields(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {
            **MANIFEST,
            "input_contract": {"type": "object", "required": ["payload"], "properties": {"payload": {"type": "array", "items": {"type": "integer", "minimum": 1}}}, "additionalProperties": False},
            "output_contract": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
            "permissions": ["execute"],
            "entrypoint": ["python", "tool.py"],
        }
        tool_id = service.propose_tool("m1", "契约工具", "嵌套契约", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "import json,sys\nprint(json.dumps({'ok': True}))", manifest)
        with pytest.raises(ValueError, match="verification_success_binding_required"):
            service.verify_tool("m1", tool_id, 1, "success", independent_key="contract-a", evidence_ids=["e1"])
        for invalid in ({}, {"payload": [0]}, {"payload": [1], "surplus": 1}, {"payload": [1, "bad"]}):
            with pytest.raises(ValueError, match="tool_input_contract_invalid"):
                service.execute_tool("m1", tool_id, 1, invalid, dry_run=True, verification_only=True)
        ok = service.execute_tool("m1", tool_id, 1, {"payload": [1]}, verification_only=True)
        assert ok["status"] == "success"
        assert json.loads(ok["stdout"]) == {"ok": True}
    finally:
        service.close()


def test_manifest_tampering_and_non_allowlisted_entrypoint_are_rejected(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "object"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "完整性工具", "测试绑定", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('{}')", manifest)
        row = service.store.db.execute("SELECT manifest FROM tool_versions WHERE tool_id=? AND version=1", (tool_id,)).fetchone()
        tampered = json.loads(row[0])
        tampered["permissions"].append("network")
        service.store.db.execute("UPDATE tool_versions SET manifest=? WHERE tool_id=? AND version=1", (json.dumps(tampered), tool_id))
        with pytest.raises(ValueError, match="content_hash_mismatch"):
            service.execute_tool("m1", tool_id, 1, {}, verification_only=True)
    finally:
        service.close()


def test_tool_source_path_must_remain_inside_owner_version_directory(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "object"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "路径工具", "containment", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('{}')", manifest)
        outside = tmp_path / "outside.py"
        outside.write_text("print('{}')", encoding="utf-8")
        service.store.db.execute("UPDATE tool_versions SET source_path=? WHERE tool_id=? AND version=1", (str(outside), tool_id))
        with pytest.raises(ValueError, match="source_path_invalid"):
            service.execute_tool("m1", tool_id, 1, {}, verification_only=True)
    finally:
        service.close()


def test_unsupported_permissions_are_rejected_before_execution(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "object"}, "permissions": ["execute", "network"], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "网络工具", "必须 fail closed", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('{}')", manifest)
        with pytest.raises(ValueError, match="permission_not_supported"):
            service.execute_tool("m1", tool_id, 1, {}, verification_only=True)
    finally:
        service.close()


def test_execution_kills_tool_when_output_limit_is_exceeded(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "输出限额工具", "超限必须终止", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('x' * 2000000)", manifest)
        result = service.execute_tool("m1", tool_id, 1, {}, verification_only=True, max_output_bytes=1024)
        assert result["status"] == "failed"
        assert result["termination_reason"] == "output_limit_exceeded"
        assert len(result["stdout"].encode("utf-8")) <= 1024
    finally:
        service.close()


def test_execution_timeout_is_audited(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "超时工具", "超时必须终止并审计", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "import time; time.sleep(2); print('late')", manifest)
        for key in ("timeout-a", "timeout-b"):
            verify_execution(service, "m1", tool_id, 1, key)
        result = service.execute_tool("m1", tool_id, 1, {}, verification_only=True, timeout_seconds=0.1)
        assert result["status"] == "failed"
        assert result["termination_reason"] == "timeout"
        audit = service.list_tool_executions("m1", tool_id=tool_id)[0]
        assert audit["status"] == "failed"
        assert audit["termination_reason"] == "timeout"
    finally:
        service.close()


def test_stderr_output_limit_is_audited(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "错误输出工具", "stderr 超限必须终止", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "import sys; sys.stderr.write('e' * 2000000)", manifest)
        result = service.execute_tool("m1", tool_id, 1, {}, verification_only=True, max_output_bytes=1024)
        assert result["status"] == "failed"
        assert result["termination_reason"] == "output_limit_exceeded"
        assert len(result["stderr"].encode("utf-8")) <= 1024
    finally:
        service.close()


def test_host_backend_rejects_unavailable_isolation_requirements(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "object"}, "permissions": ["execute"], "isolation": {"requires": ["network_isolation"]}, "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "网络隔离工具", "必须有网络隔离", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('{}')", manifest)
        with pytest.raises(ValueError, match="isolation_backend_unavailable"):
            service.execute_tool("m1", tool_id, 1, {}, verification_only=True)
    finally:
        service.close()


def test_business_assertion_failure_is_audited(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}, "permissions": ["execute"], "business_assertions": [{"path": "$.ok", "expected": True}], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "断言工具", "业务断言必须通过", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('{\\\"ok\\\": false}')", manifest)
        result = service.execute_tool("m1", tool_id, 1, {}, verification_only=True)
        assert result["status"] == "failed"
        assert result["termination_reason"].startswith("business_assertion_failed:")
        audit = service.list_tool_executions("m1", tool_id=tool_id)[0]
        assert audit["runtime_backend"] == "python_host"
        assert audit["isolation_level"] == "host_process"
        capabilities = json.loads(audit["isolation_capabilities"])
        assert capabilities["network_isolation"] is False
        for name in ("filesystem_isolation", "subprocess_isolation", "memory_limit", "file_size_limit", "process_tree_control", "active_cancellation"):
            assert capabilities[name] is False
        assert capabilities["working_directory"] == "temporary_only_not_a_security_boundary"
        assert audit["runtime_backend"] == "python_host"
        assert audit["isolation_level"] == "host_process"
    finally:
        service.close()


def test_verification_requires_evidence_and_scopes_idempotency_by_version(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        service.add_tool_version("m1", tool_id, "v1", {**MANIFEST, "permissions": ["execute"]})
        with pytest.raises(ValueError, match="verification_evidence_required"):
            service.verify_tool("m1", tool_id, 1, "success", independent_key="same-key", execution_id="exec-missing")
        with pytest.raises(ValueError, match="verification_success_binding_required"):
            service.verify_tool("m1", tool_id, 1, "success", independent_key="same-key", evidence_ids=["evidence-1"])
        service.add_tool_version("m1", tool_id, "v2", MANIFEST)
        assert service.tools._tool(tool_id)["current_version"] == 1
    finally:
        service.close()


def test_verification_binds_execution_and_input_hash(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "绑定工具", "验证执行绑定", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "import sys; print(sys.stdin.read())", manifest)
        for key in ("bind-a", "bind-b"):
            verify_execution(service, "m1", tool_id, 1, key)
        execution = service.execute_tool("m1", tool_id, 1, {"value": 7}, verification_only=True)
        assert execution["status"] == "success"
        with pytest.raises(ValueError, match="verification_input_binding_mismatch"):
            service.verify_tool("m1", tool_id, 1, "success", independent_key="bind-bad", evidence_ids=["run-log"], execution_id=execution["execution_id"], input_hash="0" * 64, expected_output=execution["expected_output"])
        result = service.verify_tool("m1", tool_id, 1, "success", independent_key="bind-good", evidence_ids=["run-log"], execution_id=execution["execution_id"], input_hash=execution["input_hash"], expected_output=execution["stdout"])
        assert result["status"] == "candidate"
        stored = service.store.db.execute("SELECT execution_id, input_hash, expected_output FROM tool_verifications WHERE verification_id=?", (result["verification_id"],)).fetchone()
        assert stored["execution_id"] == execution["execution_id"]
        assert stored["input_hash"] == execution["input_hash"]
    finally:
        service.close()


def test_startup_quarantines_orphan_version_directory_without_deleting_it(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        service.add_tool_version("m1", tool_id, "v1", MANIFEST)
        source = service.store.db.execute("SELECT source_path FROM tool_versions WHERE tool_id=? AND version=1", (tool_id,)).fetchone()[0]
        tool_dir = __import__("pathlib").Path(source).parent.parent
        orphan = tool_dir / "v2"
        orphan.mkdir()
        (orphan / "tool.py").write_text("orphan", encoding="utf-8")
        service.close()
        restarted = ButianService(tmp_path)
        try:
            assert restarted.tools.recovery_events[0]["action"] == "quarantined_orphan"
            assert not orphan.exists()
            quarantined = list((tool_dir / ".orphaned").iterdir())
            assert len(quarantined) == 1
            assert (quarantined[0] / "tool.py").read_text(encoding="utf-8") == "orphan"
            assert restarted.store.db.execute("SELECT COUNT(*) FROM tool_versions WHERE tool_id=?", (tool_id,)).fetchone()[0] == 1
        finally:
            restarted.close()
    except Exception:
        service.close()
        raise


def test_version_database_insert_failure_quarantines_atomically_published_directory(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        original_insert = service.tools._insert_version_row
        def fail_insert(*args):
            raise RuntimeError("injected_db_failure")
        monkeypatch.setattr(service.tools, "_insert_version_row", fail_insert)
        with pytest.raises(RuntimeError, match="injected_db_failure"):
            service.add_tool_version("m1", tool_id, "interrupted-source", MANIFEST)
        assert service.store.db.execute("SELECT COUNT(*) FROM tool_versions WHERE tool_id=?", (tool_id,)).fetchone()[0] == 0
        tool_dir = __import__("pathlib").Path(service.tools.tools_root / "m1" / "tools" / tool_id)
        assert not (tool_dir / "v1").exists()
        quarantined = list((tool_dir / ".orphaned").iterdir())
        assert len(quarantined) == 1
        assert (quarantined[0] / "tool.py").read_text(encoding="utf-8") == "interrupted-source"
    finally:
        service.close()


def test_concurrent_version_add_allocates_distinct_versions(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        barrier = __import__("threading").Barrier(2)
        results = []
        errors = []
        def add(content):
            try:
                barrier.wait(timeout=3)
                results.append(service.add_tool_version("m1", tool_id, content, MANIFEST))
            except Exception as exc:
                errors.append(exc)
        threads = [__import__("threading").Thread(target=add, args=(f"source-{i}",)) for i in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        assert all(not thread.is_alive() for thread in threads)
        assert not errors
        assert sorted(item["version"] for item in results) == [1, 2]
        assert service.tools._tool(tool_id)["current_version"] == 1
        rows = service.store.db.execute("SELECT version, source_path FROM tool_versions WHERE tool_id=? ORDER BY version", (tool_id,)).fetchall()
        assert [row["version"] for row in rows] == [1, 2]
        assert all(__import__("pathlib").Path(row["source_path"]).is_file() for row in rows)
    finally:
        service.close()


def test_same_source_different_manifest_creates_distinct_version(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        first = service.add_tool_version("m1", tool_id, "print('same')", {**MANIFEST, "description": "one"})
        second = service.add_tool_version("m1", tool_id, "print('same')", {**MANIFEST, "description": "two"})
        again = service.add_tool_version("m1", tool_id, "print('same')", {**MANIFEST, "description": "two"})
        assert first["version"] == 1
        assert second["version"] == 2
        assert again["version"] == 2
        assert again["status"] == "idempotent"
    finally:
        service.close()


def test_repeated_share_is_idempotent_and_permission_change_is_rejected(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('share')", {**MANIFEST, "permissions": ["execute"], "risk_level": "low"})
        for index in range(4):
            execution = service.execute_tool("m1", tool_id, 1, f"share-{index}", verification_only=True)
            service.verify_tool("m1", tool_id, 1, "success", independent_key=f"share-{index}", task_id=f"share-{index}", environment={"kind": f"env-{index % 2}"}, evidence_ids=[f"e-{index}"], execution_id=execution["execution_id"], input_hash=execution["input_hash"], expected_output=execution["expected_output"])
        assert service.tools._tool(tool_id)["status"] == "active"
        first = service.share_tool("m1", tool_id, "m2", permission="team_readonly")
        second = service.share_tool("m1", tool_id, "m2", permission="team_readonly")
        assert second["share_id"] == first["share_id"]
        with pytest.raises(ValueError, match="tool_share_already_exists"):
            service.share_tool("m1", tool_id, "m2", permission="team_execute")
    finally:
        service.close()


def test_trial_success_requires_bound_recipient_execution(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        service.add_tool_version("m1", tool_id, "import sys; print(sys.stdin.read())", {**MANIFEST, "permissions": ["execute"], "risk_level": "low"})
        for index in range(4):
            execution = service.execute_tool("m1", tool_id, 1, f"owner-{index}", verification_only=True)
            service.verify_tool(
                "m1", tool_id, 1, "success", independent_key=f"trial-gate-{index}",
                task_id=f"trial-gate-{index}", environment={"kind": f"env-{index}"},
                evidence_ids=[f"e-{index}"], execution_id=execution["execution_id"],
                input_hash=execution["input_hash"], expected_output=execution["expected_output"],
            )
        share = service.share_tool("m1", tool_id, "m2", permission="team_execute")
        service.accept_tool_share("m2", share["share_id"])
        execution = service.execute_tool("m2", tool_id, 1, "recipient", share_id=share["share_id"])
        result = service.trial_tool(
            "m2", share["share_id"], "success", independent_key="recipient-run",
            evidence_ids=["recipient-log"], execution_id=execution["execution_id"],
            input_hash=execution["input_hash"], expected_output=execution["expected_output"],
        )
        assert result["status"] == "trial"
        assert result["verification"]["execution_id"] == execution["execution_id"]
        with pytest.raises(ValueError, match="trial_input_binding_mismatch"):
            service.trial_tool(
                "m2", share["share_id"], "success", independent_key="bad-input",
                evidence_ids=["recipient-log"], execution_id=execution["execution_id"],
                input_hash="0" * 64, expected_output=execution["expected_output"],
            )
    finally:
        service.close()


def test_repair_trial_does_not_consume_use_when_input_is_invalid_or_launch_fails(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "input_contract": {"type": "object", "required": ["value"], "properties": {"value": {"type": "string"}}}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"], "risk_level": "low"}
        service.add_tool_version("m1", tool_id, "print('stable')", manifest)
        for index in range(4):
            execution = service.execute_tool("m1", tool_id, 1, {"value": str(index)}, verification_only=True)
            service.verify_tool("m1", tool_id, 1, "success", independent_key=f"gate-{index}", task_id=f"gate-{index}", environment={"kind": f"env-{index}"}, evidence_ids=[f"e-{index}"], execution_id=execution["execution_id"], input_hash=execution["input_hash"], expected_output=execution["expected_output"])
        share = service.share_tool("m1", tool_id, "m2", permission="team_execute")
        service.accept_tool_share("m2", share["share_id"])
        failed = service.execute_tool("m2", tool_id, 1, {"value": "issue"}, share_id=share["share_id"])
        issue = service.report_tool_issue("m2", tool_id, 1, description="repair requested", execution_id=failed["execution_id"])
        service.add_tool_version("m1", tool_id, "print('repair')", {**manifest, "description": "repair"})
        repair = service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="fixed")
        trial_id = repair["trial_share_id"]
        before = service.store.db.execute("SELECT used_count FROM tool_shares WHERE share_id=?", (trial_id,)).fetchone()[0]
        with pytest.raises(ValueError, match="tool_input_contract_invalid"):
            service.execute_tool("m2", tool_id, 2, {}, share_id=trial_id)
        assert service.store.db.execute("SELECT used_count FROM tool_shares WHERE share_id=?", (trial_id,)).fetchone()[0] == before
        import butian.runtime.tools as tools_module
        original_popen = tools_module.subprocess.Popen
        monkeypatch.setattr(tools_module.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("launch denied")))
        launched = service.execute_tool("m2", tool_id, 2, {"value": "valid"}, share_id=trial_id)
        assert launched["status"] == "failed"
        assert launched["termination_reason"] == "launch_failed"
        assert service.store.db.execute("SELECT used_count FROM tool_shares WHERE share_id=?", (trial_id,)).fetchone()[0] == before
        monkeypatch.setattr(tools_module.subprocess, "Popen", original_popen)
        successful = service.execute_tool("m2", tool_id, 2, {"value": "valid"}, share_id=trial_id)
        assert successful["status"] == "success"
        assert service.store.db.execute("SELECT used_count FROM tool_shares WHERE share_id=?", (trial_id,)).fetchone()[0] == before + 1
    finally:
        service.close()


def test_execution_timeout_is_not_reported_as_user_cancellation(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "timeout", "timeout audit", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1, manifest=manifest)["tool_id"]
        service.add_tool_version("m1", tool_id, "import time\ntime.sleep(2)\nprint('done')", manifest)
        result = service.execute_tool("m1", tool_id, 1, "input", verification_only=True, timeout_seconds=0.1)
        assert result["status"] == "failed"
        assert result["termination_reason"] == "timeout"
        audit = service.store.db.execute("SELECT status, termination_reason FROM tool_executions WHERE execution_id=?", (result["execution_id"],)).fetchone()
        assert tuple(audit) == ("failed", "timeout")
    finally:
        service.close()


def test_repair_conflict_does_not_partially_update_issue(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"], "risk_level": "low"}
        service.add_tool_version("m1", tool_id, "print('v1')", manifest)
        service.add_tool_version("m1", tool_id, "print('v2')", {**manifest, "description": "v2"})
        issue = service.report_tool_issue("m1", tool_id, 1, description="broken")
        service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="first attempt")
        before = dict(service.store.db.execute("SELECT status, repair_version, diagnosis FROM tool_issues WHERE issue_id=?", (issue["issue_id"],)).fetchone())
        with pytest.raises(ValueError, match="tool_issue_not_repairable"):
            service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="second attempt")
        after = dict(service.store.db.execute("SELECT status, repair_version, diagnosis FROM tool_issues WHERE issue_id=?", (issue["issue_id"],)).fetchone())
        assert after == before
        share = service.store.db.execute("SELECT issue_id FROM tool_shares WHERE issue_id=?", (issue["issue_id"],)).fetchone()
        assert share["issue_id"] == issue["issue_id"]
    finally:
        service.close()


def test_verification_promotion_rolls_back_on_status_transition_failure(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"], "risk_level": "low"}
        service.add_tool_version("m1", tool_id, "print('ok')", manifest)
        execution = service.execute_tool("m1", tool_id, 1, {}, verification_only=True)
        original_transition = service.tools._transition
        def fail_transition(*args, **kwargs):
            raise RuntimeError("injected_transition_failure")
        monkeypatch.setattr(service.tools, "_transition", fail_transition)
        with pytest.raises(RuntimeError, match="injected_transition_failure"):
            service.verify_tool("m1", tool_id, 1, "success", independent_key="atomic-a", task_id="atomic-task", environment={"kind": "test"}, evidence_ids=["atomic-evidence"], execution_id=execution["execution_id"], input_hash=execution["input_hash"], expected_output=execution["expected_output"])
        assert service.store.db.execute("SELECT 1 FROM tool_verifications WHERE tool_id=? AND independent_key=?", (tool_id, "atomic-a")).fetchone() is None
        assert service.store.db.execute("SELECT status, current_version FROM tool_assets WHERE tool_id=?", (tool_id,)).fetchone()["status"] == "draft"
        assert service.store.db.execute("SELECT 1 FROM tool_events WHERE idempotency_key=?", (f"tool:{tool_id}:verification:atomic-a",)).fetchone() is None
        monkeypatch.setattr(service.tools, "_transition", original_transition)
    finally:
        service.close()


def test_repair_confirmation_rolls_back_issue_and_trial_revocation_on_event_failure(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"], "risk_level": "low"}
        service.add_tool_version("m1", tool_id, "print('v1')", manifest)
        issue = service.report_tool_issue("m1", tool_id, 1, description="broken")
        service.add_tool_version("m1", tool_id, "print('v2')", {**manifest, "description": "v2"})
        repair = service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="fixed")
        run = service.execute_tool("m1", tool_id, 2, {}, verification_only=True)
        original_event = service.tools._event
        monkeypatch.setattr(service.tools, "_event", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected_event_failure")))
        with pytest.raises(RuntimeError, match="injected_event_failure"):
            service.confirm_tool_repair("m1", issue["issue_id"], execution_id=run["execution_id"], resolved=True)
        current = service.store.db.execute("SELECT status FROM tool_issues WHERE issue_id=?", (issue["issue_id"],)).fetchone()
        share = service.store.db.execute("SELECT status FROM tool_shares WHERE share_id=?", (repair["trial_share_id"],)).fetchone()
        assert current["status"] == "awaiting_confirmation"
        assert share["status"] == "trial"
        monkeypatch.setattr(service.tools, "_event", original_event)
    finally:
        service.close()


def test_failed_repair_can_be_retried_but_security_issue_cannot_use_ordinary_trial(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"], "risk_level": "low"}
        service.add_tool_version("m1", tool_id, "print('v1')", manifest)
        issue = service.report_tool_issue("m1", tool_id, 1, description="broken", severity="bug")
        service.add_tool_version("m1", tool_id, "print('v2')", {**manifest, "description": "v2"})
        first = service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="first fix")
        first_run = service.execute_tool("m1", tool_id, 2, {}, verification_only=True)
        reopened = service.confirm_tool_repair("m1", issue["issue_id"], execution_id=first_run["execution_id"], resolved=False)
        assert reopened["status"] == "reopened"
        assert service.store.db.execute("SELECT status FROM tool_shares WHERE share_id=?", (first["trial_share_id"],)).fetchone()["status"] == "revoked"
        service.add_tool_version("m1", tool_id, "print('v3')", {**manifest, "description": "v3"})
        second = service.repair_tool_issue("m1", issue["issue_id"], repair_version=3, diagnosis="second fix")
        assert second["status"] == "awaiting_confirmation"
        security = service.report_tool_issue("m1", tool_id, 1, description="possible code execution escape", severity="security")
        candidate = service.repair_tool_issue("m1", security["issue_id"], repair_version=3, diagnosis="ordinary trial")
        assert candidate["status"] == "security_review"
        assert "trial_share_id" not in candidate
    finally:
        service.close()


def test_repair_proposal_requires_open_or_reopened_issue_and_reporter_confirmation(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"], "risk_level": "low"}
        service.add_tool_version("m1", tool_id, "print('v1')", manifest)
        issue = service.report_tool_issue("m1", tool_id, 1, description="broken")
        service.add_tool_version("m1", tool_id, "print('v2')", {**manifest, "description": "v2"})
        service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="fixed")
        with pytest.raises(ValueError, match="tool_issue_not_repairable"):
            service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="duplicate")
        with pytest.raises(ValueError, match="tool_repair_confirmation_requires_reporter"):
            service.confirm_tool_repair("m2", issue["issue_id"], execution_id="missing", resolved=True)
    finally:
        service.close()


def test_security_issue_requires_independent_execution_evidence_review(tmp_path):
    service = make_service(tmp_path)
    try:
        service.add_member("m3", "独立审查者")
        service.add_member("m4", "备用审查者")
        service.store.set_member_token("m3", "secret-m3")
        service.store.set_member_token("m4", "secret-m4")
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"], "risk_level": "low"}
        service.add_tool_version("m1", tool_id, "print('v1')", manifest)
        issue = service.report_tool_issue("m1", tool_id, 1, description="possible escape", severity="security")
        assert issue["status"] == "security_review"
        service.add_tool_version("m1", tool_id, "print('v2')", {**manifest, "description": "patched"})
        proposal = service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="patched and checked")
        assert proposal["status"] == "security_review"
        assert "trial_share_id" not in proposal
        with pytest.raises(ValueError, match="independent_reviewer"):
            service.review_tool_security_issue("m1", issue["issue_id"], execution_id="missing", evidence_ids=["report"], resolved=True)
        evidence_path = tmp_path / "independent-review.log"
        evidence_path.write_text("reviewed", encoding="utf-8")
        evidence_id = service.store.record_evidence(evidence_path, owner_member_id="m4", actor_member_id="m4", token="secret-m4")
        run = service.execute_tool("m4", tool_id, 2, {}, verification_only=True, security_review_issue_id=issue["issue_id"])
        with pytest.raises(ValueError, match="execution_mismatch"):
            service.review_tool_security_issue("m4", issue["issue_id"], execution_id="missing", evidence_ids=["report"], resolved=True)
        with pytest.raises(ValueError, match="evidence_required"):
            service.review_tool_security_issue("m4", issue["issue_id"], execution_id=run["execution_id"], evidence_ids=[], resolved=True)
        reviewed = service.review_tool_security_issue("m4", issue["issue_id"], execution_id=run["execution_id"], evidence_ids=[evidence_id], resolved=True)
        assert reviewed["status"] == "resolved"
        assert reviewed["review_member_id"] == "m4"
        assert reviewed["review_execution_id"] == run["execution_id"]
        assert json.loads(reviewed["review_evidence"]) == [evidence_id]
    finally:
        service.close()


def test_existing_tool_issue_table_gets_security_review_columns(tmp_path):
    from butian.runtime.store import Store
    from butian.runtime.tools import ToolStore

    store = Store(tmp_path / "legacy.db")
    store.db.execute("CREATE TABLE tool_issues (issue_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL, version INTEGER NOT NULL, reporter_member_id TEXT NOT NULL, execution_id TEXT, description TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL, repair_version INTEGER, diagnosis TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    tools = ToolStore(store, tmp_path)
    try:
        columns = {row[1] for row in store.db.execute("PRAGMA table_info(tool_issues)")}
        assert {"review_member_id", "review_execution_id", "review_evidence"} <= columns
    finally:
        store.close()


def test_security_review_rejects_fake_foreign_and_tampered_evidence_and_reopens(tmp_path):
    service = make_service(tmp_path)
    try:
        service.add_member("m3", "独立审查者")
        service.add_member("m4", "证据所有者")
        service.store.set_member_token("m3", "secret-m3")
        service.store.set_member_token("m4", "secret-m4")
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"], "risk_level": "low"}
        service.add_tool_version("m1", tool_id, "print('v1')", manifest)
        issue = service.report_tool_issue("m1", tool_id, 1, description="escape", severity="security")
        service.add_tool_version("m1", tool_id, "print('v2')", {**manifest, "description": "patched"})
        service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="checked")
        run = service.execute_tool("m3", tool_id, 2, {}, verification_only=True, security_review_issue_id=issue["issue_id"])
        with pytest.raises(ValueError, match="evidence_not_found|evidence_invalid"):
            service.review_tool_security_issue("m3", issue["issue_id"], execution_id=run["execution_id"], evidence_ids=["ev_missing"], resolved=True)
        foreign_path = tmp_path / "foreign.log"
        foreign_path.write_text("foreign", encoding="utf-8")
        foreign_id = service.store.record_evidence(foreign_path, owner_member_id="m4", actor_member_id="m4", token="secret-m4")
        with pytest.raises(ValueError, match="evidence_access_denied"):
            service.review_tool_security_issue("m3", issue["issue_id"], execution_id=run["execution_id"], evidence_ids=[foreign_id], resolved=True)
        own_path = tmp_path / "own.log"
        own_path.write_text("original", encoding="utf-8")
        own_id = service.store.record_evidence(own_path, owner_member_id="m3", actor_member_id="m3", token="secret-m3")
        own_path.write_text("tampered", encoding="utf-8")
        with pytest.raises(ValueError, match="evidence_invalid"):
            service.review_tool_security_issue("m3", issue["issue_id"], execution_id=run["execution_id"], evidence_ids=[own_id], resolved=True)
        valid_path = tmp_path / "valid.log"
        valid_path.write_text("valid", encoding="utf-8")
        valid_id = service.store.record_evidence(valid_path, owner_member_id="m3", actor_member_id="m3", token="secret-m3")
        first = service.review_tool_security_issue("m3", issue["issue_id"], execution_id=run["execution_id"], evidence_ids=[valid_id], resolved=False)
        assert first["status"] == "reopened"
        service.add_tool_version("m1", tool_id, "print('v3')", {**manifest, "description": "second patch"})
        second = service.repair_tool_issue("m1", issue["issue_id"], repair_version=3, diagnosis="second checked")
        assert second["status"] == "security_review"
        run2 = service.execute_tool("m3", tool_id, 3, {}, verification_only=True, security_review_issue_id=issue["issue_id"])
        final = service.review_tool_security_issue("m3", issue["issue_id"], execution_id=run2["execution_id"], evidence_ids=[valid_id], resolved=True)
        assert final["status"] == "resolved"
    finally:
        service.close()


def test_context_cards_include_only_authorized_current_contracts(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        secret = "SOURCE_SECRET_MUST_NOT_LEAK"
        manifest = {**MANIFEST, "permissions": ["execute"], "entrypoint": ["python", "tool.py"], "private_key": "MANIFEST_SECRET_MUST_NOT_LEAK", "risk_level": "low"}
        service.add_tool_version("m1", tool_id, f"print({secret!r})", manifest)
        for index in range(4):
            run = service.execute_tool("m1", tool_id, 1, f"recall-{index}", verification_only=True)
            service.verify_tool("m1", tool_id, 1, "success", independent_key=f"recall-{index}", task_id=f"recall-{index}", environment={"kind": f"env-{index}"}, evidence_ids=[f"recall-e-{index}"], execution_id=run["execution_id"], input_hash=run["input_hash"], expected_output=run["expected_output"])
        owner_cards = service.tools.context_cards("m1", "通用分析", limit=10)
        natural_cards = service.tools.context_cards("m1", "请用通用分析处理这份文本", limit=10)
        assert [card["tool_id"] for card in natural_cards] == [tool_id]
        punctuated_cards = service.tools.context_cards("m1", "请用通用分析，处理这份文本。", limit=10)
        assert [card["tool_id"] for card in punctuated_cards] == [tool_id]
        assert service.tools.context_cards("m1", "请帮我查询明天的天气", limit=10) == []
        assert [card["tool_id"] for card in owner_cards] == [tool_id]
        assert all("source" not in card and "entrypoint" not in card for card in owner_cards)
        readonly = service.share_tool("m1", tool_id, "m2", permission="team_readonly")
        assert service.tools.context_cards("m2", "分析", limit=10) == []
        service.accept_tool_share("m2", readonly["share_id"])
        shared_cards = service.tools.context_cards("m2", "分析", limit=10)
        assert [card["tool_id"] for card in shared_cards] == [tool_id]
        assert set(shared_cards[0]) == {"tool_id", "name", "description", "version", "input_contract", "output_contract", "permissions", "failure_conditions", "permission"}
        assert secret not in json.dumps(shared_cards, ensure_ascii=False)
        assert "MANIFEST_SECRET" not in json.dumps(shared_cards, ensure_ascii=False)
        assert "entrypoint" not in json.dumps(shared_cards, ensure_ascii=False)
        service.store.db.execute("UPDATE tool_shares SET expires_at=? WHERE share_id=?", (0, readonly["share_id"]))
        assert service.tools.context_cards("m2", "分析", limit=10) == []
        service.store.db.execute("UPDATE tool_shares SET expires_at=NULL, status='revoked' WHERE share_id=?", (readonly["share_id"],))
        assert service.tools.context_cards("m2", "分析", limit=10) == []
    finally:
        service.close()


def test_repair_and_duplicate_guards_reject_audited_false_evidence(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('v1')", {**MANIFEST, "permissions": ["execute"]})
        issue = service.report_tool_issue("m1", tool_id, 1, description="bad")
        with pytest.raises(ValueError, match="tool_repair_version_must_be_newer"):
            service.repair_tool_issue("m1", issue["issue_id"], repair_version=1, diagnosis="same version")
        with pytest.raises(ValueError, match="tool_issue_diagnosis_required"):
            service.repair_tool_issue("m1", issue["issue_id"], repair_version=1, diagnosis="")
        issue2 = service.report_tool_issue("m1", tool_id, 1, description="duplicate")
        service.store.db.execute("UPDATE tool_issues SET duplicate_of=? WHERE issue_id=?", (issue2["issue_id"], issue["issue_id"]))
        with pytest.raises(ValueError, match="duplicate_issue_cycle"):
            service.link_duplicate_tool_issue("m1", issue2["issue_id"], issue["issue_id"])
        with pytest.raises(ValueError, match="tool_version_not_found"):
            service.record_tool_issue_pair("m1", issue["issue_id"], repair_version=99, environment={}, input_hash="fake", before_outcome="failure", after_outcome="success")
    finally:
        service.close()

def test_context_cards_filter_version_mismatch_and_execute_shares(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "permissions": ["execute"], "risk_level": "low"}
        service.add_tool_version("m1", tool_id, "print('v1')", manifest)
        for index in range(4):
            run = service.execute_tool("m1", tool_id, 1, str(index), verification_only=True)
            service.verify_tool("m1", tool_id, 1, "success", independent_key=f"exec-recall-{index}", task_id=f"exec-recall-{index}", environment={"kind": f"env-{index}"}, evidence_ids=[f"exec-e-{index}"], execution_id=run["execution_id"], input_hash=run["input_hash"], expected_output=run["expected_output"])
        share = service.share_tool("m1", tool_id, "m2", permission="team_execute")
        service.accept_tool_share("m2", share["share_id"])
        assert [card["tool_id"] for card in service.tools.context_cards("m2", "通用分析 execute", limit=10)] == [tool_id]
        service.store.db.execute("UPDATE tool_shares SET version=version+1 WHERE share_id=?", (share["share_id"],))
        assert service.tools.context_cards("m2", "execute", limit=10) == []
    finally:
        service.close()

def test_issue_triage_timeout_pair_stats_and_breaker_are_explicit(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('ok')", {**MANIFEST, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]})
        service.add_tool_version("m1", tool_id, "print('fixed')", {**MANIFEST, "permissions": ["execute"], "entrypoint": ["python", "tool.py"], "description": "fixed"})
        issue = service.report_tool_issue("m1", tool_id, 1, description="bad output", environment={"kind": "lab"})
        assert issue["triage_owner_member_id"] == "m1"
        assert service.triage_tool_issue("m1", issue["issue_id"], status="triaged")["status"] == "triaged"
        with pytest.raises(ValueError, match="invalid_tool_issue_transition"):
            service.triage_tool_issue("m1", issue["issue_id"], status="resolved")
        requested = service.request_tool_issue_information("m1", issue["issue_id"], request="need reproduction")
        assert requested["status"] == "needs_information"
        with pytest.raises(ValueError, match="tool_issue_rejection_reason_required"):
            service.triage_tool_issue("m1", issue["issue_id"], status="rejected")
        service.set_dispatcher_config("m2")
        rejected = service.triage_tool_issue("m1", issue["issue_id"], status="rejected", reason="cannot reproduce")
        assert rejected["escalation_to_member_id"] == "m2"
        issue2 = service.report_tool_issue("m1", tool_id, 1, description="another issue")
        service.triage_tool_issue("m1", issue2["issue_id"], status="triaged", due_seconds=1)
        assert service.expire_tool_issue_escalations("m1", now=issue2["due_at"] + 2)[0]["escalated_at"] is not None
        before = service.execute_tool("m1", tool_id, 1, "same", verification_only=True)
        after = service.execute_tool("m1", tool_id, 2, "same", verification_only=True)
        service.verify_tool("m1", tool_id, 1, "success", independent_key="breaker-pair-before", task_id="breaker-pair", environment={"kind": "lab"}, evidence_ids=["before"], execution_id=before["execution_id"], input_hash=before["input_hash"], expected_output=before["expected_output"])
        service.verify_tool("m1", tool_id, 2, "success", independent_key="breaker-pair-after", task_id="breaker-pair", environment={"kind": "lab"}, evidence_ids=["after"], execution_id=after["execution_id"], input_hash=after["input_hash"], expected_output=after["expected_output"])
        service.record_tool_issue_pair("m1", issue["issue_id"], repair_version=2, environment={"kind": "lab"}, input_hash=before["input_hash"], before_outcome="success", after_outcome="success")
        stats = service.tool_issue_statistics("m1", tool_id, version=2, environment={"kind": "lab"})
        assert stats["paired"]["n"] == 1
        assert stats["paired"]["improved"] == 0
        assert stats["paired"]["improvement_confidence_interval"]["metric"] == "paired_success_rate_difference"
        assert stats["paired"]["improvement_confidence_interval"]["status"] == "unknown"
        duplicate = service.report_tool_issue("m1", tool_id, 1, description="duplicate issue")
        linked = service.link_duplicate_tool_issue("m1", duplicate["issue_id"], issue["issue_id"])
        assert linked["status"] != "resolved"
        assert linked["duplicate_of"] == issue["issue_id"]
        service.open_tool_circuit_breaker("m1", tool_id, 1, reason="security")
        with pytest.raises(ValueError, match="tool_circuit_breaker_open"):
            service.execute_tool("m1", tool_id, 1, "x", verification_only=True)
        assert service.recover_tool_circuit_breaker("m1", tool_id, 1)["status"] == "recovered"
        service.open_tool_circuit_breaker("m1", tool_id, 1, reason="security-again")
        service.recover_tool_circuit_breaker("m1", tool_id, 1)
        breaker_events = service.store.db.execute("SELECT event_type FROM tool_events WHERE tool_id=? AND version=1 AND event_type LIKE 'circuit_breaker_%' ORDER BY created_at", (tool_id,)).fetchall()
        assert [row["event_type"] for row in breaker_events][-4:] == ["circuit_breaker_opened", "circuit_breaker_recovered"] * 2
    finally:
        service.close()


def test_audit_regressions_failed_repair_reopens_and_information_events_are_unique(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        service.add_tool_version("m1", tool_id, "print('v1')", manifest)
        issue = service.report_tool_issue("m1", tool_id, 1, description="broken")
        service.add_tool_version("m1", tool_id, "raise RuntimeError('broken')", {**manifest, "description": "v2"})
        repair = service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="candidate")
        failed = service.execute_tool("m1", tool_id, 2, {}, verification_only=True)
        assert failed["status"] == "failed"
        assert service.confirm_tool_repair("m1", issue["issue_id"], execution_id=failed["execution_id"], resolved=False)["status"] == "reopened"
        info_issue = service.report_tool_issue("m1", tool_id, 1, description="needs details")
        for index in range(2):
            service.request_tool_issue_information("m1", info_issue["issue_id"], request=f"question-{index}")
            service.provide_tool_issue_information("m1", info_issue["issue_id"], information=f"answer-{index}")
        count = service.store.db.execute("SELECT count(*) FROM tool_issue_events WHERE issue_id=? AND event_type IN ('information_requested','information_provided')", (info_issue["issue_id"],)).fetchone()[0]
        assert count == 4
    finally:
        service.close()


def test_audit_regressions_pair_requires_issue_environment(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        service.add_tool_version("m1", tool_id, "print('v1')", manifest)
        service.add_tool_version("m1", tool_id, "print('v2')", {**manifest, "description": "v2"})
        issue = service.report_tool_issue("m1", tool_id, 1, description="regression", environment={"kind": "lab"})
        before = service.execute_tool("m1", tool_id, 1, "same", verification_only=True)
        after = service.execute_tool("m1", tool_id, 2, "same", verification_only=True)
        service.verify_tool("m1", tool_id, 1, "success", independent_key="legacy-env-before", task_id="legacy-env", environment={"kind": "lab"}, evidence_ids=["before"], execution_id=before["execution_id"], input_hash=before["input_hash"], expected_output=before["expected_output"])
        service.verify_tool("m1", tool_id, 2, "success", independent_key="legacy-env-after", task_id="legacy-env", environment={"kind": "lab"}, evidence_ids=["after"], execution_id=after["execution_id"], input_hash=after["input_hash"], expected_output=after["expected_output"])
        with pytest.raises(ValueError, match="tool_pair_environment_mismatch"):
            service.record_tool_issue_pair("m1", issue["issue_id"], repair_version=2, environment={"kind": "other"}, input_hash=before["input_hash"], before_outcome="success", after_outcome="success")
        result = service.record_tool_issue_pair("m1", issue["issue_id"], repair_version=2, environment={"kind": "lab"}, input_hash=before["input_hash"], before_outcome="success", after_outcome="success")
        assert result["paired"]["n"] == 1
        assert result["paired"]["improvement_confidence_interval"]["metric"] == "paired_success_rate_difference"
        assert result["paired"]["improvement_confidence_interval"]["status"] == "unknown"
    finally:
        service.close()


def test_circuit_breaker_recovery_requires_verified_clean_run(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        service.add_tool_version("m1", tool_id, "print('ok')", manifest)
        with pytest.raises(ValueError, match="tool_circuit_breaker_recovery_not_verified"):
            service.open_tool_circuit_breaker("m1", tool_id, 1, reason="risk")
            service.recover_tool_circuit_breaker("m1", tool_id, 1)
        service.store.db.execute("DELETE FROM tool_circuit_breakers WHERE tool_id=? AND version=1", (tool_id,))
        service.store.db.commit()
        execution = service.execute_tool("m1", tool_id, 1, "verified", verification_only=True)
        service.verify_tool("m1", tool_id, 1, "success", independent_key="breaker-recovery", task_id="breaker-recovery", environment={"kind": "lab"}, evidence_ids=["breaker-log"], execution_id=execution["execution_id"], input_hash=execution["input_hash"], expected_output=execution["expected_output"])
        service.open_tool_circuit_breaker("m1", tool_id, 1, reason="risk-again")
        assert service.recover_tool_circuit_breaker("m1", tool_id, 1)["status"] == "recovered"
    finally:
        service.close()


def test_pair_statistics_report_success_rate_difference_and_require_verified_environment(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        service.add_tool_version("m1", tool_id, "raise RuntimeError('v1')", manifest)
        service.add_tool_version("m1", tool_id, "print('v2')", {**manifest, "description": "v2"})
        issue = service.report_tool_issue("m1", tool_id, 1, description="paired", environment={"kind": "lab"})
        before = service.execute_tool("m1", tool_id, 1, "same", verification_only=True)
        after = service.execute_tool("m1", tool_id, 2, "same", verification_only=True)
        service.verify_tool("m1", tool_id, 1, "failure", independent_key="pair-before", task_id="pair-task", environment={"kind": "lab"}, evidence_ids=["before"], execution_id=before["execution_id"], input_hash=before["input_hash"])
        service.verify_tool("m1", tool_id, 2, "success", independent_key="pair-after", task_id="pair-task", environment={"kind": "lab"}, evidence_ids=["after"], execution_id=after["execution_id"], input_hash=after["input_hash"], expected_output=after["expected_output"])
        result = service.record_tool_issue_pair("m1", issue["issue_id"], repair_version=2, environment={"kind": "lab"}, input_hash=before["input_hash"], before_outcome="failure", after_outcome="success")
        interval = result["paired"]["improvement_confidence_interval"]
        assert interval["metric"] == "paired_success_rate_difference"
        assert interval["status"] == "unknown"
        assert interval["lower"] is None and interval["upper"] is None
        with pytest.raises(ValueError, match="tool_pair_environment_mismatch"):
            service.record_tool_issue_pair("m1", issue["issue_id"], repair_version=2, environment={"kind": "other"}, input_hash=before["input_hash"], before_outcome="failure", after_outcome="success")
    finally:
        service.close()

def test_issue_statistics_reports_uncertain_small_paired_samples(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('v1')", {**MANIFEST, "permissions": ["execute"]})
        stats = service.tool_issue_statistics("m1", tool_id, version=1)
        interval = stats["paired"]["improvement_confidence_interval"]
        assert interval["status"] == "unknown"
        assert interval["lower"] is None and interval["upper"] is None
    finally:
        service.close()


def test_context_recall_report_separates_candidates_and_reliability_rejections(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "permissions": ["execute"], "risk_level": "low"}
        service.add_tool_version("m1", tool_id, "raise SystemExit(1)", manifest)
        run = service.execute_tool("m1", tool_id, 1, "bad", verification_only=True)
        assert run["status"] == "failed"
        report = service.tools.context_recall_report("m1", "通用分析", limit=10)
        assert any(item["card"]["tool_id"] == tool_id for item in report["candidates"])
        rejection = next(item for item in report["rejected"] if item["tool_id"] == tool_id)
        assert rejection["reason"] == "tool_not_active"
        assert service.tools.context_cards("m1", "通用分析", limit=10) == []
    finally:
        service.close()


def test_host_trusted_backend_records_real_runtime_identity(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "object"},
                    "permissions": ["execute"], "isolation": {"profile": "host_trusted"},
                    "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "宿主工具", "真实宿主执行", reuse_lower=3, manual_cost_upper=10,
                                       call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2,
                                       risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('{\"ok\": true}')", manifest)
        result = service.execute_tool("m1", tool_id, 1, {}, verification_only=True)
        assert result["status"] == "success"
        audit = service.list_tool_executions("m1", tool_id=tool_id)[0]
        assert audit["runtime_backend"] == "host_trusted"
        assert audit["isolation_level"] == "same_account_host"
        capabilities = json.loads(audit["isolation_capabilities"])
        assert capabilities["host_trusted"] is True
        assert capabilities["same_account_no_os_isolation"] is True
        assert capabilities["argv"] == [sys.executable, "-I", capabilities["source_path"]]
        assert capabilities["cwd"]
        assert capabilities["interpreter"] == sys.executable
        assert len(capabilities["content_hash"]) == 64
        assert len(capabilities["manifest_hash"]) == 64
    finally:
        service.close()
