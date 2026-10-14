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


def registered_tool_proofs(service, actor, labels):
    import hashlib
    evidence_ids = []
    directory = service.store.path.parent / 'verification-proofs'
    directory.mkdir(exist_ok=True)
    for label in labels:
        digest = hashlib.sha256((actor + ':' + label).encode()).hexdigest()
        path = directory / (digest + '.txt')
        row = service.store.db.execute('SELECT evidence_id FROM evidence WHERE path=? AND owner_member_id=?', (str(path.resolve()), actor)).fetchone()
        if row:
            evidence_ids.append(row[0])
        else:
            path.write_text('Independent observation: ' + label, encoding='utf-8')
            evidence_ids.append(service.record_evidence(path, owner_member_id=actor, actor_member_id=actor))
    return evidence_ids

def verify_execution(service, owner, tool_id, version, key, input_data=None):
    execution = execute_in_task(service, owner, tool_id, version, input_data or {}, verification_only=True)
    return service.verify_tool(owner, tool_id, version, "success", independent_key=key,
                               task_id=execution_task_id(service, execution["execution_id"]), environment={"kind": "unit"},
                               evidence_ids=registered_tool_proofs(service, owner, [f"evidence-{key}"]), execution_id=execution["execution_id"],
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
        execution_a = execute_in_task(service, "m1", tool_id, 1, "input-a", verification_only=True)
        execution_b = execute_in_task(service, "m1", tool_id, 1, "input-b", verification_only=True)
        first = service.verify_tool("m1", tool_id, 1, "success", independent_key="case-a", task_id=execution_task_id(service, execution_a["execution_id"]), environment={"kind": "unit"}, evidence_ids=registered_tool_proofs(service, "m1", ["log-a"]), execution_id=execution_a["execution_id"], input_hash=execution_a["input_hash"], expected_output=execution_a["expected_output"])
        assert first["status"] == "candidate"
        second = service.verify_tool("m1", tool_id, 1, "success", independent_key="case-b", task_id=execution_task_id(service, execution_b["execution_id"]), environment={"kind": "integration"}, evidence_ids=registered_tool_proofs(service, "m1", ["log-b"]), execution_id=execution_b["execution_id"], input_hash=execution_b["input_hash"], expected_output=execution_b["expected_output"])
        assert second["status"] == "candidate"
        execution_c = execute_in_task(service, "m1", tool_id, 1, "input-c", verification_only=True)
        third = service.verify_tool("m1", tool_id, 1, "success", independent_key="case-c", task_id=execution_task_id(service, execution_c["execution_id"]), environment={"kind": "staging"}, evidence_ids=registered_tool_proofs(service, "m1", ["log-c"]), execution_id=execution_c["execution_id"], input_hash=execution_c["input_hash"], expected_output=execution_c["expected_output"])
        assert third["status"] == "candidate"
        execution_d = execute_in_task(service, "m1", tool_id, 1, "input-d", verification_only=True)
        fourth = service.verify_tool("m1", tool_id, 1, "success", independent_key="case-d", task_id=execution_task_id(service, execution_d["execution_id"]), environment={"kind": "unit"}, evidence_ids=registered_tool_proofs(service, "m1", ["log-d"]), execution_id=execution_d["execution_id"], input_hash=execution_d["input_hash"], expected_output=execution_d["expected_output"])
        assert fourth["status"] == "active"
        duplicate = service.verify_tool("m1", tool_id, 1, "success", independent_key="case-a", task_id=execution_task_id(service, execution_a["execution_id"]), environment={"kind": "unit"}, evidence_ids=registered_tool_proofs(service, "m1", ["log-a"]), execution_id=execution_a["execution_id"], input_hash=execution_a["input_hash"], expected_output=execution_a["expected_output"])
        assert duplicate["verification_id"] == first["verification_id"]
        with pytest.raises(ValueError, match="verification_idempotency_conflict"):
            service.verify_tool("m1", tool_id, 1, "failure", independent_key="case-a", environment={"kind": "unit"}, evidence_ids=registered_tool_proofs(service, "m1", ["conflict-log"]))
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
            execution = execute_in_task(service, "m1", tool_id, 1, f"input-{i}", verification_only=True)
            service.verify_tool("m1", tool_id, 1, "success", independent_key=f"legacy-{i}", task_id=execution_task_id(service, execution["execution_id"]), environment={"kind": f"env-{i}"}, evidence_ids=registered_tool_proofs(service, "m1", [f"log-{i}"]), execution_id=execution["execution_id"], input_hash=execution["input_hash"], expected_output=execution["expected_output"])
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
            service.verify_tool("m1", tool_id, 1, "success", independent_key="unbound", evidence_ids=registered_tool_proofs(service, "m1", ["e"]))
        service.verify_tool("m1", tool_id, 1, "unknown", independent_key="unknown-1", evidence_ids=registered_tool_proofs(service, "m1", ["e"]))
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
        service.verify_tool("m1", tool_id, 1, "unknown", independent_key="candidate-observation", evidence_ids=registered_tool_proofs(service, "m1", ["candidate-log"]))
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
        preview = execute_in_task(service, "m1", tool_id, 1, {"x": 1}, dry_run=True, verification_only=True)
        assert preview["status"] == "dry_run"
        result = execute_in_task(service, "m1", tool_id, 1, {"x": 1}, verification_only=True)
        assert result["status"] == "success"
        assert '"x": 1' in result["stdout"]
        audit = service.list_tool_executions("m1", tool_id=tool_id)
        assert [row["status"] for row in audit][:2] == ["success", "dry_run"]
        with pytest.raises(ValueError, match="tool_execute_denied"):
            execute_in_task(service, "m2", tool_id, 1, {})
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
            result = execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
            assert result["status"] == "failed"
        service.verify_tool("m1", tool_id, 1, "unknown", independent_key="failure-observation", evidence_ids=registered_tool_proofs(service, "m1", ["failure-log"]))
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
        service.add_tool_version("m1", tool_id, "import sys; print(sys.stdin.read())", {**MANIFEST, "permissions": ["cpu", "execute"], "risk_level": "low"})
        assert service.tools._tool(tool_id)["status"] == "draft"
        with pytest.raises(ValueError, match="only_active"):
            service.share_tool("m1", tool_id, "m2")
        for key in ("activate-a", "activate-b", "activate-c", "activate-d"):
            run = execute_in_task(service, "m1", tool_id, 1, "activation", verification_only=True)
            service.verify_tool("m1", tool_id, 1, "success", independent_key=key, task_id=execution_task_id(service, run["execution_id"]), environment={"kind": key}, execution_id=run["execution_id"], input_hash=run["input_hash"], expected_output=run["expected_output"], evidence_ids=registered_tool_proofs(service, "m1", [key]))
        assert service.tools._tool(tool_id)["status"] == "active"
        share = service.share_tool("m1", tool_id, "m2", permission="team_execute")
        accepted = service.accept_tool_share("m2", share["share_id"])
        assert accepted["status"] == "trial"
        trial_run = execute_in_task(service, "m2", tool_id, 1, "trial", share_id=share["share_id"])
        trial = service.trial_tool("m2", share["share_id"], "success", independent_key="trial-a", task_id=execution_task_id(service, trial_run["execution_id"]), environment={"kind": "trial"}, evidence_ids=registered_tool_proofs(service, "m2", ["trial-evidence"]), execution_id=trial_run["execution_id"], input_hash=trial_run["input_hash"], expected_output=trial_run["expected_output"])
        assert trial["status"] == "trial"
        archived = service.archive_tool("m1", tool_id, reason="cold")
        assert archived["status"] == "archived"
        restored = service.restore_tool("m1", tool_id)
        assert restored["status"] == "candidate"
        for index, key in enumerate(("restore-a", "restore-b", "restore-c", "restore-d")):
            restore_run = execute_in_task(service, "m1", tool_id, 1, "restore", verification_only=index == 0)
            service.verify_tool("m1", tool_id, 1, "success", independent_key=key, task_id=execution_task_id(service, restore_run["execution_id"]), environment={"kind": key}, evidence_ids=registered_tool_proofs(service, "m1", [key]), execution_id=restore_run["execution_id"], input_hash=restore_run["input_hash"], expected_output=restore_run["expected_output"])
        assert service.list_tools("m1")[0]["status"] == "active"
    finally:
        service.close()

def test_repair_preserves_stable_version_and_requires_real_confirmation(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        service.add_tool_version("m1", tool_id, "print('ok')", manifest)
        run_v1 = execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
        service.verify_tool("m1", tool_id, 1, "success", independent_key="repair-a", task_id=execution_task_id(service, run_v1["execution_id"]), environment={"kind": "test"}, evidence_ids=registered_tool_proofs(service, "m1", ["repair-log-a"]), execution_id=run_v1["execution_id"], input_hash=run_v1["input_hash"], expected_output=run_v1["expected_output"])
        assert service.tools._tool(tool_id)["status"] == "candidate"
        issue = service.report_tool_issue("m1", tool_id, 1, description="Wrong boundary handling")
        service.add_tool_version("m1", tool_id, "print('fixed')", {**manifest, "description": "v2"})
        assert service.tools._tool(tool_id)["current_version"] == 1
        service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="Correct boundary")
        dry = execute_in_task(service, "m1", tool_id, 2, {}, dry_run=True, verification_only=True)
        with pytest.raises(ValueError, match="execution_mismatch"):
            service.confirm_tool_repair("m1", issue["issue_id"], execution_id=dry["execution_id"], resolved=True)
        run_v2 = execute_in_task(service, "m1", tool_id, 2, {}, verification_only=True)
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
            execution = execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
            service.verify_tool("m1", tool_id, 1, "success", independent_key=f"stable-{index}", task_id=execution_task_id(service, execution["execution_id"]), environment={"kind": f"env-{index}"}, evidence_ids=registered_tool_proofs(service, "m1", [f"stable-log-{index}"]), execution_id=execution["execution_id"], input_hash=execution["input_hash"], expected_output=execution["expected_output"])
        share = service.share_tool("m1", tool_id, "m2", permission="team_execute")
        service.accept_tool_share("m2", share["share_id"])
        failed = execute_in_task(service, "m2", tool_id, 1, {}, share_id=share["share_id"])
        issue = service.report_tool_issue("m2", tool_id, 1, description="Observed incorrect result", execution_id=failed["execution_id"])
        service.add_tool_version("m1", tool_id, "print('repaired')", {**manifest, "description": "repair"})
        repair = service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="Fixed result")
        repaired = execute_in_task(service, "m2", tool_id, 2, {}, share_id=repair["trial_share_id"])
        trial = service.trial_tool("m2", repair["trial_share_id"], "success", independent_key="repair-success", evidence_ids=registered_tool_proofs(service, "m2", ["repair-evidence"]), execution_id=repaired["execution_id"], input_hash=repaired["input_hash"], expected_output=repaired["expected_output"])
        assert trial["outcome"] == "success"
        assert service.confirm_tool_repair("m2", issue["issue_id"], execution_id=repaired["execution_id"], resolved=True)["status"] == "resolved"
        with pytest.raises(ValueError, match="tool_execute_denied|tool_trial"):
            execute_in_task(service, "m2", tool_id, 2, {}, share_id=repair["trial_share_id"])
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
            service.verify_tool("owner", tid, 1, "success", independent_key="i1", evidence_ids=registered_tool_proofs(service, "owner", ["e1"]))
        result = execute_in_task(service, "owner", tid, 1, {}, verification_only=True)
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
            service.verify_tool("m1", tool_id, 1, "success", independent_key="contract-a", evidence_ids=registered_tool_proofs(service, "m1", ["e1"]))
        for invalid in ({}, {"payload": [0]}, {"payload": [1], "surplus": 1}, {"payload": [1, "bad"]}):
            with pytest.raises(ValueError, match="tool_input_contract_invalid"):
                execute_in_task(service, "m1", tool_id, 1, invalid, dry_run=True, verification_only=True)
        ok = execute_in_task(service, "m1", tool_id, 1, {"payload": [1]}, verification_only=True)
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
            execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
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
            execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
    finally:
        service.close()


def test_unsupported_permissions_are_rejected_before_execution(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "object"}, "permissions": ["execute", "network"], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "网络工具", "必须 fail closed", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('{}')", manifest)
        with pytest.raises(ValueError, match="permission_not_supported"):
            execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
    finally:
        service.close()


def test_execution_kills_tool_when_output_limit_is_exceeded(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "输出限额工具", "超限必须终止", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('x' * 2000000)", manifest)
        result = execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True, max_output_bytes=1024)
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
        result = execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True, timeout_seconds=0.1)
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
        result = execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True, max_output_bytes=1024)
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
            execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
    finally:
        service.close()


def test_business_assertion_failure_is_audited(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}, "permissions": ["execute"], "business_assertions": [{"path": "$.ok", "expected": True}], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "断言工具", "业务断言必须通过", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('{\\\"ok\\\": false}')", manifest)
        result = execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
        assert result["status"] == "failed"
        assert result["termination_reason"].startswith("business_assertion_failed:")
        audit = service.list_tool_executions("m1", tool_id=tool_id)[0]
        assert audit["runtime_backend"] == "unified_executor"
        assert audit["isolation_level"] == "host_process"
        capabilities = json.loads(audit["isolation_capabilities"])
        assert capabilities["network_isolation"] is False
        assert capabilities["process_tree_control"] is True
        assert capabilities["active_cancellation"] is True
        assert capabilities["tree_exit_confirmed"] is True
        assert capabilities["executor_id"]
        assert capabilities["instance_id"]
        for name in ("filesystem_isolation", "subprocess_isolation", "memory_limit", "file_size_limit"):
            assert capabilities[name] is False
        assert capabilities["working_directory"] == "retained_execution_directory_not_a_security_boundary"
        assert audit["runtime_backend"] == "unified_executor"
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
            service.verify_tool("m1", tool_id, 1, "success", independent_key="same-key", evidence_ids=registered_tool_proofs(service, "m1", ["evidence-1"]))
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
        execution = execute_in_task(service, "m1", tool_id, 1, {"value": 7}, verification_only=True)
        assert execution["status"] == "success"
        with pytest.raises(ValueError, match="verification_input_binding_mismatch"):
            service.verify_tool("m1", tool_id, 1, "success", independent_key="bind-bad", evidence_ids=registered_tool_proofs(service, "m1", ["run-log"]), execution_id=execution["execution_id"], input_hash="0" * 64, expected_output=execution["expected_output"])
        result = service.verify_tool("m1", tool_id, 1, "success", independent_key="bind-good", evidence_ids=registered_tool_proofs(service, "m1", ["run-log"]), execution_id=execution["execution_id"], input_hash=execution["input_hash"], expected_output=execution["stdout"])
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
            execution = execute_in_task(service, "m1", tool_id, 1, f"share-{index}", verification_only=True)
            service.verify_tool("m1", tool_id, 1, "success", independent_key=f"share-{index}", task_id=execution_task_id(service, execution["execution_id"]), environment={"kind": f"env-{index % 2}"}, evidence_ids=registered_tool_proofs(service, "m1", [f"e-{index}"]), execution_id=execution["execution_id"], input_hash=execution["input_hash"], expected_output=execution["expected_output"])
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
            execution = execute_in_task(service, "m1", tool_id, 1, f"owner-{index}", verification_only=True)
            service.verify_tool(
                "m1", tool_id, 1, "success", independent_key=f"trial-gate-{index}",
                task_id=execution_task_id(service, execution["execution_id"]), environment={"kind": f"env-{index}"},
                evidence_ids=registered_tool_proofs(service, "m1", [f"e-{index}"]), execution_id=execution["execution_id"],
                input_hash=execution["input_hash"], expected_output=execution["expected_output"],
            )
        share = service.share_tool("m1", tool_id, "m2", permission="team_execute")
        service.accept_tool_share("m2", share["share_id"])
        execution = execute_in_task(service, "m2", tool_id, 1, "recipient", share_id=share["share_id"])
        result = service.trial_tool(
            "m2", share["share_id"], "success", independent_key="recipient-run",
            evidence_ids=registered_tool_proofs(service, "m2", ["recipient-log"]), execution_id=execution["execution_id"],
            input_hash=execution["input_hash"], expected_output=execution["expected_output"],
        )
        assert result["status"] == "trial"
        assert result["verification"]["execution_id"] == execution["execution_id"]
        with pytest.raises(ValueError, match="trial_input_binding_mismatch"):
            service.trial_tool(
                "m2", share["share_id"], "success", independent_key="bad-input",
                evidence_ids=registered_tool_proofs(service, "m2", ["recipient-log"]), execution_id=execution["execution_id"],
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
            execution = execute_in_task(service, "m1", tool_id, 1, {"value": str(index)}, verification_only=True)
            service.verify_tool("m1", tool_id, 1, "success", independent_key=f"gate-{index}", task_id=execution_task_id(service, execution["execution_id"]), environment={"kind": f"env-{index}"}, evidence_ids=registered_tool_proofs(service, "m1", [f"e-{index}"]), execution_id=execution["execution_id"], input_hash=execution["input_hash"], expected_output=execution["expected_output"])
        share = service.share_tool("m1", tool_id, "m2", permission="team_execute")
        service.accept_tool_share("m2", share["share_id"])
        failed = execute_in_task(service, "m2", tool_id, 1, {"value": "issue"}, share_id=share["share_id"])
        issue = service.report_tool_issue("m2", tool_id, 1, description="repair requested", execution_id=failed["execution_id"])
        service.add_tool_version("m1", tool_id, "print('repair')", {**manifest, "description": "repair"})
        repair = service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="fixed")
        trial_id = repair["trial_share_id"]
        before = service.store.db.execute("SELECT used_count FROM tool_shares WHERE share_id=?", (trial_id,)).fetchone()[0]
        with pytest.raises(ValueError, match="tool_input_contract_invalid"):
            execute_in_task(service, "m2", tool_id, 2, {}, share_id=trial_id)
        assert service.store.db.execute("SELECT used_count FROM tool_shares WHERE share_id=?", (trial_id,)).fetchone()[0] == before
        original_runner = service.tools.execution_runner
        install_launch_failure_runner(service, 'launch denied')
        launched = execute_in_task(service, "m2", tool_id, 2, {"value": "valid"}, share_id=trial_id)
        assert launched["status"] == "failed"
        assert launched["termination_reason"] == "launch_failed"
        assert service.store.db.execute("SELECT used_count FROM tool_shares WHERE share_id=?", (trial_id,)).fetchone()[0] == before
        service.tools.execution_runner = original_runner
        successful = execute_in_task(service, "m2", tool_id, 2, {"value": "valid"}, share_id=trial_id)
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
        result = execute_in_task(service, "m1", tool_id, 1, "input", verification_only=True, timeout_seconds=0.1)
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
        execution = execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
        original_transition = service.tools._transition
        def fail_transition(*args, **kwargs):
            raise RuntimeError("injected_transition_failure")
        monkeypatch.setattr(service.tools, "_transition", fail_transition)
        with pytest.raises(RuntimeError, match="injected_transition_failure"):
            service.verify_tool("m1", tool_id, 1, "success", independent_key="atomic-a", task_id=execution_task_id(service, execution["execution_id"]), environment={"kind": "test"}, evidence_ids=registered_tool_proofs(service, "m1", ["atomic-evidence"]), execution_id=execution["execution_id"], input_hash=execution["input_hash"], expected_output=execution["expected_output"])
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
        run = execute_in_task(service, "m1", tool_id, 2, {}, verification_only=True)
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
        first_run = execute_in_task(service, "m1", tool_id, 2, {}, verification_only=True)
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
        run = execute_in_task(service, "m4", tool_id, 2, {}, verification_only=True, security_review_issue_id=issue["issue_id"])
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
        run = execute_in_task(service, "m3", tool_id, 2, {}, verification_only=True, security_review_issue_id=issue["issue_id"])
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
        run2 = execute_in_task(service, "m3", tool_id, 3, {}, verification_only=True, security_review_issue_id=issue["issue_id"])
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
            run = execute_in_task(service, "m1", tool_id, 1, f"recall-{index}", verification_only=True)
            service.verify_tool("m1", tool_id, 1, "success", independent_key=f"recall-{index}", task_id=execution_task_id(service, run["execution_id"]), environment={"kind": f"env-{index}"}, evidence_ids=registered_tool_proofs(service, "m1", [f"recall-e-{index}"]), execution_id=run["execution_id"], input_hash=run["input_hash"], expected_output=run["expected_output"])
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
            run = execute_in_task(service, "m1", tool_id, 1, str(index), verification_only=True)
            service.verify_tool("m1", tool_id, 1, "success", independent_key=f"exec-recall-{index}", task_id=execution_task_id(service, run["execution_id"]), environment={"kind": f"env-{index}"}, evidence_ids=registered_tool_proofs(service, "m1", [f"exec-e-{index}"]), execution_id=run["execution_id"], input_hash=run["input_hash"], expected_output=run["expected_output"])
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
        before = execute_in_task(service, "m1", tool_id, 1, "same", verification_only=True)
        after = execute_in_task(service, "m1", tool_id, 2, "same", verification_only=True)
        service.verify_tool("m1", tool_id, 1, "success", independent_key="breaker-pair-before", task_id=execution_task_id(service, before["execution_id"]), environment={"kind": "lab"}, evidence_ids=registered_tool_proofs(service, "m1", ["before"]), execution_id=before["execution_id"], input_hash=before["input_hash"], expected_output=before["expected_output"])
        service.verify_tool("m1", tool_id, 2, "success", independent_key="breaker-pair-after", task_id=execution_task_id(service, after["execution_id"]), environment={"kind": "lab"}, evidence_ids=registered_tool_proofs(service, "m1", ["after"]), execution_id=after["execution_id"], input_hash=after["input_hash"], expected_output=after["expected_output"])
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
            execute_in_task(service, "m1", tool_id, 1, "x", verification_only=True)
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
        failed = execute_in_task(service, "m1", tool_id, 2, {}, verification_only=True)
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
        before = execute_in_task(service, "m1", tool_id, 1, "same", verification_only=True)
        after = execute_in_task(service, "m1", tool_id, 2, "same", verification_only=True)
        service.verify_tool("m1", tool_id, 1, "success", independent_key="legacy-env-before", task_id=execution_task_id(service, before["execution_id"]), environment={"kind": "lab"}, evidence_ids=registered_tool_proofs(service, "m1", ["before"]), execution_id=before["execution_id"], input_hash=before["input_hash"], expected_output=before["expected_output"])
        service.verify_tool("m1", tool_id, 2, "success", independent_key="legacy-env-after", task_id=execution_task_id(service, after["execution_id"]), environment={"kind": "lab"}, evidence_ids=registered_tool_proofs(service, "m1", ["after"]), execution_id=after["execution_id"], input_hash=after["input_hash"], expected_output=after["expected_output"])
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
        execution = execute_in_task(service, "m1", tool_id, 1, "verified", verification_only=True)
        service.verify_tool("m1", tool_id, 1, "success", independent_key="breaker-recovery", task_id=execution_task_id(service, execution["execution_id"]), environment={"kind": "lab"}, evidence_ids=registered_tool_proofs(service, "m1", ["breaker-log"]), execution_id=execution["execution_id"], input_hash=execution["input_hash"], expected_output=execution["expected_output"])
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
        before = execute_in_task(service, "m1", tool_id, 1, "same", verification_only=True)
        after = execute_in_task(service, "m1", tool_id, 2, "same", verification_only=True)
        service.verify_tool("m1", tool_id, 1, "failure", independent_key="pair-before", task_id=execution_task_id(service, before["execution_id"]), environment={"kind": "lab"}, evidence_ids=registered_tool_proofs(service, "m1", ["before"]), execution_id=before["execution_id"], input_hash=before["input_hash"])
        service.verify_tool("m1", tool_id, 2, "success", independent_key="pair-after", task_id=execution_task_id(service, after["execution_id"]), environment={"kind": "lab"}, evidence_ids=registered_tool_proofs(service, "m1", ["after"]), execution_id=after["execution_id"], input_hash=after["input_hash"], expected_output=after["expected_output"])
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
        run = execute_in_task(service, "m1", tool_id, 1, "bad", verification_only=True)
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
        service.grant_host_authorization("m1", tool_id, 1, capabilities=["process.spawn"], reason="test")
        result = execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
        assert result["status"] == "success"
        audit = service.list_tool_executions("m1", tool_id=tool_id)[0]
        assert audit["runtime_backend"] == "unified_executor"
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


def test_host_trusted_requires_revocable_host_authorization(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "object"},
                    "permissions": ["execute"], "isolation": {"profile": "host_trusted"},
                    "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "授权宿主工具", "需要显式宿主授权", reuse_lower=3, manual_cost_upper=10,
                                       call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2,
                                       risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('{\"ok\": true}')", manifest)
        with pytest.raises(ValueError, match="host_authorization_required"):
            execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
        grant = service.grant_host_authorization("m1", tool_id, 1, capabilities=["process.spawn"], reason="test")
        assert grant["status"] == "active"
        result = execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
        assert result["status"] == "success"
        service.revoke_host_authorization("m1", grant["authorization_id"], reason="test revoke")
        with pytest.raises(ValueError, match="host_authorization_revoked"):
            execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
    finally:
        service.close()


def test_unavailable_backend_fails_closed_before_execution(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "object"},
                    "permissions": ["execute"], "entrypoint": ["node", "tool.js"]}
        tool_id = service.propose_tool("m1", "缺失解释器工具", "验证解释器 fail closed", reuse_lower=3, manual_cost_upper=10,
                                       call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2,
                                       risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "console.log('{}')", manifest)
        monkeypatch.setattr("butian.runtime.tools.shutil.which", lambda name: None)
        with pytest.raises(ValueError, match="tool_interpreter_unavailable:node"):
            execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
    finally:
        service.close()


def test_cancel_terminates_real_descendant_process(tmp_path):
    import os
    import threading
    import time

    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"},
                    "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "后代清理探针", "验证取消会终止真实子进程", reuse_lower=3, manual_cost_upper=10,
                                       call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2,
                                       risk_cost_upper=1)["tool_id"]
        pid_file = tmp_path / "descendant.pid"
        source = f"import pathlib, subprocess, sys, time\nchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\npathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\ntime.sleep(30)\n"
        service.add_tool_version("m1", tool_id, source, manifest)
        result = {}
        worker = threading.Thread(target=lambda: result.update(execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True, timeout_seconds=60)))
        worker.start()
        execution_id = None
        for _ in range(100):
            running = service.list_running_tool_executions("m1")
            if running:
                execution_id = running[0]["execution_id"]
                break
            time.sleep(0.02)
        assert execution_id is not None
        deadline = time.time() + 3
        child_pid = None
        while time.time() < deadline:
            if pid_file.exists():
                child_pid = int(pid_file.read_text())
                break
            time.sleep(0.02)
        assert child_pid is not None
        cancelled = service.cancel_tool_execution("m1", execution_id, reason="descendant probe")
        assert cancelled["status"] in {"cancelled", "unknown"}
        worker.join(timeout=5)
        assert not worker.is_alive()
        audit = service.store.db.execute("SELECT status, termination_reason FROM tool_executions WHERE execution_id=?", (execution_id,)).fetchone()
        assert tuple(audit) in {("cancelled", "cancelled_by_actor"), ("unknown", "cancel_unconfirmed")}
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        for _ in range(50):
            handle = kernel32.OpenProcess(0x1000, 0, child_pid)
            alive = False
            if handle:
                code = ctypes.c_uint32()
                alive = bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
                kernel32.CloseHandle(handle)
            if not alive:
                break
            time.sleep(0.05)
        else:
            assert tuple(audit) == ("unknown", "cancel_unconfirmed")
    finally:
        service.close()


def test_running_execution_can_be_cancelled_and_audited(tmp_path):
    import threading
    import time

    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "object"},
                    "permissions": ["execute"], "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "可取消工具", "验证主动取消", reuse_lower=3, manual_cost_upper=10,
                                       call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2,
                                       risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "import time\ntime.sleep(30)\n", manifest)
        result = {}
        worker = threading.Thread(target=lambda: result.update(execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True, timeout_seconds=60)))
        worker.start()
        execution_id = None
        for _ in range(100):
            running = service.list_running_tool_executions("m1")
            if running:
                execution_id = running[0]["execution_id"]
                break
            time.sleep(0.02)
        assert execution_id is not None
        cancelled = service.cancel_tool_execution("m1", execution_id, reason="test cancel")
        assert cancelled["status"] == "cancelled"
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert result["status"] == "cancelled"
        audit = service.list_tool_executions("m1", tool_id=tool_id)[0]
        assert audit["status"] == "cancelled"
        assert audit["termination_reason"] == "cancelled_by_actor"
    finally:
        service.close()


def test_execution_guard_blocks_real_process_side_effect(tmp_path):
    service = make_service(tmp_path)
    marker = tmp_path / 'must-not-exist.txt'
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, f'from pathlib import Path\nPath({str(marker)!r}).write_text("started")\nprint("ok")', {**MANIFEST, 'permissions': ['execute']})
        checks = []
        def invalid_execution():
            checks.append(True)
            raise ValueError('provider_execution_revision_conflict')
        try:
            result = service.tools.execute('m1', tool_id, 1, 'valid input', verification_only=True, execution_guard=invalid_execution)
        except ValueError as exc:
            assert str(exc) == 'provider_execution_revision_conflict'
        else:
            assert result['status'] != 'success'
        assert checks == [True]
        assert not marker.exists()
    finally:
        service.close()


def execute_in_task(service, actor, tool_id, version, input_data=None, **kwargs):
    session = service.store.create_session('solo', actor, None, {})
    task = service.store.create_task(session, '真实工具测试任务', assignee_id=actor)
    return service.execute_tool(actor, tool_id, version, input_data, task_id=task, **kwargs)


def execution_task_id(service, execution_id):
    return service.store.db.execute('SELECT task_id FROM tool_executions WHERE execution_id=?', (execution_id,)).fetchone()[0]


def test_tool_timeout_cleanup_unconfirmed_never_waits_without_bound(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from butian.runtime.execution_contract import CapabilityEvidence, ExecutionCapabilities, ExecutionObservation, REQUIRED_CONTROL
    service = make_service(tmp_path)
    caps = ExecutionCapabilities('fault-owner', 'fault-instance', {
        name: CapabilityEvidence('supported', 'fault_fixture_only', 1, 'fault-instance')
        for name in REQUIRED_CONTROL})
    class Runner:
        adapter = SimpleNamespace(capabilities=lambda: caps)
        execution_id = None
        exit_confirmed = False
        def run(self, request, *, authorization_valid):
            assert authorization_valid()
            self.execution_id = request.execution_id
            return ExecutionObservation(request.execution_id, 'fault-owner', 'fault-instance',
                2, 'unknown', reason='cleanup_unconfirmed')
        def unresolved(self):
            return [] if self.exit_confirmed or self.execution_id is None else [self.execution_id]
        def wait(self, timeout):
            assert timeout is not None, 'stop unconfirmed must not wait without bound'
            return self.exit_confirmed
    runner = Runner()
    service.tools.execution_runner = runner
    try:
        manifest = {**MANIFEST, 'input_contract': {'type': 'object'}, 'output_contract': {'type': 'text'}, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py']}
        tool_id = service.propose_tool('m1', 'Bounded timeout', 'Keep unknown after stop denial', reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)['tool_id']
        service.add_tool_version('m1', tool_id, "print('unused')", manifest)
        def forbidden_spawn(*args, **kwargs):
            raise AssertionError('fault test must use unified executor seam')
        monkeypatch.setattr('butian.runtime.tools.subprocess.Popen', forbidden_spawn)
        result = execute_in_task(service, 'm1', tool_id, 1, {}, verification_only=True, timeout_seconds=0.1)
        assert result['status'] == 'unknown'
        assert result['termination_reason'] == 'cleanup_unconfirmed'
        assert service.list_tool_executions('m1', tool_id=tool_id)[0]['status'] == 'unknown'
        assert result['execution_id'] in runner.unresolved()
        assert service.tools.wait_for_executions(timeout=0.02) is False
        runner.exit_confirmed = True
        assert service.tools.wait_for_executions(timeout=0.1) is True
    finally:
        runner.exit_confirmed = True
        service.close()

@pytest.mark.parametrize("independent_connection", [False, True])
def test_host_authorization_revoked_during_admission_never_spawns(tmp_path, monkeypatch, independent_connection):
    from butian.runtime import tools as module
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "input_contract": {"type": "object"},
                    "output_contract": {"type": "object"}, "permissions": ["execute"],
                    "isolation": {"profile": "host_trusted"},
                    "host_capabilities": ["process.spawn"], "entrypoint": ["python", "tool.py"]}
        tool_id = propose(service)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('{}')", manifest)
        grant = service.grant_host_authorization("m1", tool_id, 1,
                                                  capabilities=["process.spawn"], reason="regression")
        original_which = module.shutil.which
        def revoke_before_spawn(name):
            if independent_connection:
                import sqlite3
                with sqlite3.connect(service.store.path) as connection:
                    connection.execute("UPDATE host_authorizations SET status='revoked' WHERE authorization_id=?", (grant["authorization_id"],))
            else:
                service.revoke_host_authorization("m1", grant["authorization_id"], reason="admission regression")
            return original_which(name)
        monkeypatch.setattr(module.shutil, "which", revoke_before_spawn)
        def forbidden_spawn(*args, **kwargs):
            pytest.fail("revoked host authorization reached Popen")
        monkeypatch.setattr(module.subprocess, "Popen", forbidden_spawn)
        result = execute_in_task(service, "m1", tool_id, 1, {}, verification_only=True)
        assert result["status"] == "failed"
        audit = service.list_tool_executions("m1", tool_id=tool_id)[0]
        assert "host_authorization_revoked" in audit["stderr"]
        assert audit["exit_code"] is None
    finally:
        service.close()


def test_repair_trial_exhausted_during_admission_never_spawns(tmp_path, monkeypatch):
    from butian.runtime import tools as module
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "input_contract": {"type": "object"},
                    "output_contract": {"type": "text"}, "permissions": ["execute"],
                    "entrypoint": ["python", "tool.py"]}
        service.add_tool_version("m1", tool_id, "print('v1')", manifest)
        service.add_tool_version("m1", tool_id, "print('v2')", manifest)
        service.store.db.execute("UPDATE tool_assets SET status='active' WHERE tool_id=?", (tool_id,))
        service.share_tool("m1", tool_id, "m2", permission="team_execute")
        issue = service.report_tool_issue("m2", tool_id, 1, description="repair requested")
        repair = service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="fixed")
        share_id = repair["trial_share_id"]
        original_mkdtemp = module.tempfile.mkdtemp
        def exhaust_before_spawn(*args, **kwargs):
            service.store.db.execute("UPDATE tool_shares SET used_count=max_uses WHERE share_id=?", (share_id,))
            return original_mkdtemp(*args, **kwargs)
        monkeypatch.setattr(module.tempfile, "mkdtemp", exhaust_before_spawn)
        monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: pytest.fail("exhausted trial reached Popen"))
        result = execute_in_task(service, "m2", tool_id, 2, {}, share_id=share_id)
        assert result["status"] == "failed"
        audit = service.list_tool_executions("m2", tool_id=tool_id)[0]
        assert "tool_trial_exhausted" in audit["stderr"]
        assert audit["exit_code"] is None
    finally:
        service.close()


def test_circuit_breaker_opened_during_admission_never_spawns(tmp_path, monkeypatch):
    from butian.runtime import tools as module
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('ok')", {**MANIFEST, "permissions": ["execute"]})
        original_mkdtemp = module.tempfile.mkdtemp
        def open_before_spawn(*args, **kwargs):
            service.open_tool_circuit_breaker("m1", tool_id, 1, reason="admission regression")
            return original_mkdtemp(*args, **kwargs)
        monkeypatch.setattr(module.tempfile, "mkdtemp", open_before_spawn)
        monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: pytest.fail("open circuit breaker reached Popen"))
        result = execute_in_task(service, "m1", tool_id, 1, "input", verification_only=True)
        assert result["status"] == "failed"
        assert "tool_circuit_breaker_open" in result["stderr"]
        assert result["exit_code"] is None
    finally:
        service.close()


def install_launch_failure_runner(service, message):
    """Inject a pre-dispatch failure without starting an OS process."""
    from types import SimpleNamespace
    from butian.runtime.execution_contract import CapabilityEvidence, ExecutionCapabilities, REQUIRED_CONTROL
    caps = ExecutionCapabilities('fault-owner', 'fault-instance', {
        name: CapabilityEvidence('supported', 'fault_fixture_only', 1, 'fault-instance')
        for name in REQUIRED_CONTROL})
    class Runner:
        adapter = SimpleNamespace(capabilities=lambda: caps)
        def run(self, request, *, authorization_valid):
            assert authorization_valid()
            raise OSError(message)
        def unresolved(self): return []
        def wait(self, timeout): return True
    service.tools.execution_runner = Runner()


def test_repair_trial_uncommitted_dispatch_preserves_outer_transaction(tmp_path, monkeypatch):
    from butian.runtime import tools as module
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        manifest = {**MANIFEST, "input_contract": {"type": "object"}, "output_contract": {"type": "text"}, "permissions": ["execute"]}
        service.add_tool_version("m1", tool_id, "print('v1')", manifest)
        service.add_tool_version("m1", tool_id, "print('v2')", manifest)
        service.store.db.execute("UPDATE tool_assets SET status='active' WHERE tool_id=?", (tool_id,))
        service.share_tool("m1", tool_id, "m2", permission="team_execute")
        issue = service.report_tool_issue("m2", tool_id, 1, description="repair requested")
        repair = service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="fixed")
        share_id = repair["trial_share_id"]
        session = service.store.create_session('solo', 'm2', None, {})
        task = service.store.create_task(session, 'outer transaction launch', assignee_id='m2')
        service.store.db.execute("BEGIN IMMEDIATE")
        service.store.db.execute("UPDATE tool_shares SET max_uses=7 WHERE share_id=?", (share_id,))
        install_launch_failure_runner(service, 'must not dispatch')
        monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('fault test spawned a process'))
        with pytest.raises(ValueError, match='execution_dispatch_requires_committed_transaction'):
            service.execute_tool('m2', tool_id, 2, {}, task_id=task, share_id=share_id)
        assert service.store.db.in_transaction
        row = service.store.db.execute("SELECT used_count,max_uses FROM tool_shares WHERE share_id=?", (share_id,)).fetchone()
        assert tuple(row) == (0, 7)
        service.store.db.execute("ROLLBACK")
        assert service.store.db.execute("SELECT max_uses FROM tool_shares WHERE share_id=?", (share_id,)).fetchone()[0] != 7
    finally:
        if service.store.db.in_transaction:
            service.store.db.execute("ROLLBACK")
        service.close()


def test_tool_archived_during_admission_never_spawns(tmp_path, monkeypatch):
    from butian.runtime import tools as module
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('ok')", {**MANIFEST, 'permissions': ['execute']})
        original_mkdtemp = module.tempfile.mkdtemp
        def archive_before_spawn(*args, **kwargs):
            service.archive_tool('m1', tool_id)
            return original_mkdtemp(*args, **kwargs)
        monkeypatch.setattr(module.tempfile, 'mkdtemp', archive_before_spawn)
        monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('archived tool reached Popen'))
        result = execute_in_task(service, 'm1', tool_id, 1, 'input', verification_only=True)
        assert result['status'] == 'failed'
        assert 'tool_version_state_changed' in result['stderr']
    finally:
        service.close()


def test_execution_result_transaction_excludes_other_writers(tmp_path, monkeypatch):
    import threading
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('ok')", {**MANIFEST, 'permissions': ['execute']})
        original_event = service.tools._event
        probes = []
        def event_with_competing_writer(tool, version, event_type, payload, key):
            if event_type == 'execution':
                acquired = []
                def competing_writer():
                    held = service.store._write_lock.acquire(timeout=0.05)
                    acquired.append(held)
                    if held:
                        service.store._write_lock.release()
                thread = threading.Thread(target=competing_writer)
                thread.start()
                thread.join(timeout=1)
                assert not thread.is_alive()
                probes.extend(acquired)
            return original_event(tool, version, event_type, payload, key)
        monkeypatch.setattr(service.tools, '_event', event_with_competing_writer)
        result = execute_in_task(service, 'm1', tool_id, 1, 'input', verification_only=True)
        assert result['status'] == 'success'
        assert probes == [False], 'another writer entered the execution result transaction'
    finally:
        service.close()


def test_tool_source_changed_during_admission_never_spawns(tmp_path, monkeypatch):
    from pathlib import Path
    from butian.runtime import tools as module
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('ok')", {**MANIFEST, 'permissions': ['execute']})
        source = Path(service.store.db.execute('SELECT source_path FROM tool_versions WHERE tool_id=? AND version=1', (tool_id,)).fetchone()[0])
        original = module.tempfile.mkdtemp
        def change_before_spawn(*args, **kwargs):
            source.write_text("print('changed')", encoding='utf-8')
            return original(*args, **kwargs)
        monkeypatch.setattr(module.tempfile, 'mkdtemp', change_before_spawn)
        monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('changed tool source reached Popen'))
        result = execute_in_task(service, 'm1', tool_id, 1, 'input', verification_only=True)
        assert result['status'] == 'failed'
        assert 'tool_content_hash_mismatch' in result['stderr']
        assert result['exit_code'] is None
    finally:
        service.close()


def test_execution_started_transaction_excludes_other_writers(tmp_path, monkeypatch):
    import threading
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('ok')", {**MANIFEST, 'permissions': ['execute']})
        original = service.tools._event
        probes = []
        def competing_event(tool, version, event_type, payload, key):
            if event_type == 'execution_started':
                def writer():
                    held = service.store._write_lock.acquire(timeout=0.05)
                    probes.append(held)
                    if held:
                        service.store._write_lock.release()
                thread = threading.Thread(target=writer)
                thread.start()
                thread.join(timeout=1)
                assert not thread.is_alive()
            return original(tool, version, event_type, payload, key)
        monkeypatch.setattr(service.tools, '_event', competing_event)
        result = execute_in_task(service, 'm1', tool_id, 1, 'input', verification_only=True)
        assert result['status'] == 'success'
        assert probes == [False]
    finally:
        service.close()


def test_dry_run_audit_failure_leaves_no_partial_execution(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('ok')", {**MANIFEST, 'permissions': ['execute']})
        original = service.tools._event
        def fail_event(tool, version, event_type, payload, key):
            if event_type == 'execution_dry_run':
                raise RuntimeError('dry_run_audit_fault')
            return original(tool, version, event_type, payload, key)
        monkeypatch.setattr(service.tools, '_event', fail_event)
        with pytest.raises(RuntimeError, match='dry_run_audit_fault'):
            execute_in_task(service, 'm1', tool_id, 1, 'input', verification_only=True, dry_run=True)
        assert service.store.db.execute('SELECT COUNT(*) FROM tool_executions WHERE tool_id=?', (tool_id,)).fetchone()[0] == 0
        assert not service.store.db.in_transaction
    finally:
        service.close()


def test_issue_audit_failure_leaves_no_partial_issue(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('ok')", {**MANIFEST, 'permissions': ['execute']})
        original = service.tools._issue_event
        def fail_event(*args, **kwargs):
            raise RuntimeError('issue_audit_fault')
        monkeypatch.setattr(service.tools, '_issue_event', fail_event)
        with pytest.raises(RuntimeError, match='issue_audit_fault'):
            service.tools.report_issue('m1', tool_id, 1, description='Observed failure')
        assert service.store.db.execute('SELECT COUNT(*) FROM tool_issues WHERE tool_id=?', (tool_id,)).fetchone()[0] == 0
        assert service.store.db.execute("SELECT COUNT(*) FROM tool_events WHERE tool_id=? AND event_type='issue_reported'", (tool_id,)).fetchone()[0] == 0
        assert not service.store.db.in_transaction
    finally:
        service.close()


def test_launch_failure_record_excludes_other_writers(tmp_path, monkeypatch):
    import threading
    from butian.runtime import tools as module
    service = make_service(tmp_path)
    probes = []
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('ok')", {**MANIFEST, 'permissions': ['execute']})
        install_launch_failure_runner(service, 'controlled_launch_failure')
        monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('fault test spawned a process'))
        def trace(statement):
            if statement.startswith('INSERT INTO tool_executions'):
                def writer():
                    held = service.store._write_lock.acquire(timeout=0.05)
                    probes.append(held)
                    if held:
                        service.store._write_lock.release()
                thread = threading.Thread(target=writer)
                thread.start()
                thread.join(timeout=1)
        service.store.db.set_trace_callback(trace)
        result = execute_in_task(service, 'm1', tool_id, 1, 'input', verification_only=True)
        assert result['status'] == 'failed'
        assert 'controlled_launch_failure' in result['stderr']
        assert probes == [False], 'launch failure record admitted a competing writer'
    finally:
        service.store.db.set_trace_callback(None)
        service.close()


@pytest.mark.parametrize('operation', ['dry_run', 'report_issue'])
@pytest.mark.parametrize('audit_fails', [False, True])
def test_short_tool_writes_preserve_outer_transaction(tmp_path, monkeypatch, operation, audit_fails):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('ok')", {**MANIFEST, 'permissions': ['execute']})
        # Prepare the execution task before opening the caller's transaction.
        session_id = service.store.create_session('solo', 'm1', None, {})
        task_id = service.store.create_task(session_id, 'Outer transaction dry run', assignee_id='m1') if operation == 'dry_run' else None
        service.store.db.execute('CREATE TABLE caller_tool_probe(value TEXT)')
        service.store.db.execute('BEGIN IMMEDIATE')
        service.store.db.execute("INSERT INTO caller_tool_probe VALUES ('pending')")
        if audit_fails:
            original = service.tools._event
            def fault(tool, version, event_type, payload, key):
                if event_type in {'execution_dry_run', 'issue_reported'}:
                    raise RuntimeError('outer_tool_audit_fault')
                return original(tool, version, event_type, payload, key)
            monkeypatch.setattr(service.tools, '_event', fault)
        def invoke():
            if operation == 'report_issue':
                return service.tools.report_issue('m1', tool_id, 1, description='Observed failure')
            return service.execute_tool('m1', tool_id, 1, 'input', task_id=task_id, verification_only=True, dry_run=True)
        if audit_fails:
            with pytest.raises(RuntimeError, match='outer_tool_audit_fault'):
                invoke()
        else:
            invoke()
        assert service.store.db.in_transaction
        assert service.store.db.execute('SELECT COUNT(*) FROM caller_tool_probe').fetchone()[0] == 1
        table = 'tool_executions' if operation == 'dry_run' else 'tool_issues'
        assert service.store.db.execute('SELECT COUNT(*) FROM ' + table + ' WHERE tool_id=?', (tool_id,)).fetchone()[0] == (0 if audit_fails else 1)
        service.store.db.execute('ROLLBACK')
        assert service.store.db.execute('SELECT COUNT(*) FROM caller_tool_probe').fetchone()[0] == 0
        assert service.store.db.execute('SELECT COUNT(*) FROM ' + table + ' WHERE tool_id=?', (tool_id,)).fetchone()[0] == 0
    finally:
        if service.store.db.in_transaction:
            service.store.db.execute('ROLLBACK')
        service.close()

@pytest.mark.parametrize('severity', ['bug', 'security'])
def test_repair_proposal_preserves_outer_transaction(tmp_path, severity):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('v1')", MANIFEST)
        issue = service.report_tool_issue('m1', tool_id, 1, description='repair probe', severity=severity)
        service.add_tool_version('m1', tool_id, "print('v2')", MANIFEST)
        db = service.store.db
        db.execute('CREATE TABLE repair_caller_probe (value TEXT)')
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT INTO repair_caller_probe VALUES ('pending')")
        result = service.repair_tool_issue('m1', issue['issue_id'], repair_version=2, diagnosis='controlled repair')
        assert result['repair_version'] == 2
        assert db.in_transaction
        assert db.execute('SELECT COUNT(*) FROM repair_caller_probe').fetchone()[0] == 1
        db.rollback()
        assert db.execute('SELECT repair_version FROM tool_issues WHERE issue_id=?', (issue['issue_id'],)).fetchone()[0] is None
        assert db.execute('SELECT COUNT(*) FROM repair_caller_probe').fetchone()[0] == 0
    finally:
        service.close()

@pytest.mark.parametrize('severity', ['bug', 'security'])
def test_repair_proposal_audit_failure_preserves_caller_and_rolls_back_proposal(tmp_path, monkeypatch, severity):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('v1')", MANIFEST)
        issue = service.report_tool_issue('m1', tool_id, 1, description='repair audit probe', severity=severity)
        service.add_tool_version('m1', tool_id, "print('v2')", MANIFEST)
        db = service.store.db
        db.execute('CREATE TABLE repair_failure_caller_probe (value TEXT)')
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT INTO repair_failure_caller_probe VALUES ('pending')")
        original = service.tools._event
        def fail_repair_event(tool, version, event_type, *args, **kwargs):
            if event_type in {'security_repair_proposed', 'issue_repair_proposed'}:
                raise RuntimeError('repair_audit_fault')
            return original(tool, version, event_type, *args, **kwargs)
        monkeypatch.setattr(service.tools, '_event', fail_repair_event)
        with pytest.raises(RuntimeError, match='repair_audit_fault'):
            service.repair_tool_issue('m1', issue['issue_id'], repair_version=2, diagnosis='controlled repair')
        assert db.in_transaction
        assert db.execute('SELECT COUNT(*) FROM repair_failure_caller_probe').fetchone()[0] == 1
        current = db.execute('SELECT status,repair_version FROM tool_issues WHERE issue_id=?', (issue['issue_id'],)).fetchone()
        assert current['status'] == issue['status']
        assert current['repair_version'] is None
        assert db.execute('SELECT COUNT(*) FROM tool_shares WHERE issue_id=?', (issue['issue_id'],)).fetchone()[0] == 0
        db.rollback()
    finally:
        service.close()

@pytest.mark.parametrize('severity', ['bug', 'security'])
@pytest.mark.parametrize('audit_fails', [False, True])
def test_repair_review_preserves_outer_transaction(tmp_path, monkeypatch, severity, audit_fails):
    service = make_service(tmp_path)
    try:
        service.add_member('reviewer', 'Independent reviewer')
        tool_id = propose(service)['tool_id']
        manifest = {**MANIFEST, 'input_contract': {'type': 'object'}, 'output_contract': {'type': 'text'}, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py'], 'risk_level': 'low'}
        service.add_tool_version('m1', tool_id, "print('v1')", manifest)
        issue = service.report_tool_issue('m1', tool_id, 1, description='review transaction probe', severity=severity)
        service.add_tool_version('m1', tool_id, "print('v2')", manifest)
        service.repair_tool_issue('m1', issue['issue_id'], repair_version=2, diagnosis='controlled repair')
        actor = 'reviewer' if severity == 'security' else 'm1'
        execution_options = {'security_review_issue_id': issue['issue_id']} if severity == 'security' else {}
        run = execute_in_task(service, actor, tool_id, 2, {}, verification_only=True, **execution_options)
        evidence_ids = registered_tool_proofs(service, actor, ['transaction-review'])
        db = service.store.db
        original_status = db.execute('SELECT status FROM tool_issues WHERE issue_id=?', (issue['issue_id'],)).fetchone()[0]
        db.execute('CREATE TABLE review_caller_probe (value TEXT)')
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT INTO review_caller_probe VALUES ('pending')")
        if audit_fails:
            monkeypatch.setattr(service.tools, '_event', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('review_audit_fault')))
        def review():
            if severity == 'security':
                return service.review_tool_security_issue(actor, issue['issue_id'], execution_id=run['execution_id'], evidence_ids=evidence_ids, resolved=True)
            return service.confirm_tool_repair(actor, issue['issue_id'], execution_id=run['execution_id'], resolved=True)
        if audit_fails:
            with pytest.raises(RuntimeError, match='review_audit_fault'):
                review()
        else:
            assert review()['status'] == 'resolved'
        assert db.in_transaction
        assert db.execute('SELECT COUNT(*) FROM review_caller_probe').fetchone()[0] == 1
        if audit_fails:
            assert db.execute('SELECT status FROM tool_issues WHERE issue_id=?', (issue['issue_id'],)).fetchone()[0] == original_status
        db.rollback()
        assert db.execute('SELECT status FROM tool_issues WHERE issue_id=?', (issue['issue_id'],)).fetchone()[0] == original_status
        assert db.execute('SELECT COUNT(*) FROM review_caller_probe').fetchone()[0] == 0
    finally:
        service.close()

@pytest.mark.parametrize('severity', ['bug', 'security'])
def test_repair_review_revalidates_version_after_cross_connection_update(tmp_path, severity):
    import sqlite3
    service = make_service(tmp_path)
    other = None
    try:
        service.add_member('reviewer', 'Independent reviewer')
        tool_id = propose(service)['tool_id']
        manifest = {**MANIFEST, 'input_contract': {'type': 'object'}, 'output_contract': {'type': 'text'}, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py'], 'risk_level': 'low'}
        service.add_tool_version('m1', tool_id, "print('v1')", manifest)
        issue = service.report_tool_issue('m1', tool_id, 1, description='concurrent repair probe', severity=severity)
        service.add_tool_version('m1', tool_id, "print('v2')", manifest)
        service.repair_tool_issue('m1', issue['issue_id'], repair_version=2, diagnosis='repair two')
        actor = 'reviewer' if severity == 'security' else 'm1'
        execution_options = {'security_review_issue_id': issue['issue_id']} if severity == 'security' else {}
        run = execute_in_task(service, actor, tool_id, 2, {}, verification_only=True, **execution_options)
        evidence_ids = registered_tool_proofs(service, actor, ['cross-connection-review'])
        service.add_tool_version('m1', tool_id, "print('v3')", manifest)
        other = sqlite3.connect(service.store.path, isolation_level=None)
        changed = []
        def replace_repair_before_writer_reservation(statement):
            if not changed and (statement.startswith('SAVEPOINT ') or statement == 'BEGIN'):
                changed.append(True)
                other.execute('UPDATE tool_issues SET repair_version=3 WHERE issue_id=?', (issue['issue_id'],))
        service.store.db.set_trace_callback(replace_repair_before_writer_reservation)
        with pytest.raises(ValueError, match='execution_mismatch'):
            if severity == 'security':
                service.review_tool_security_issue(actor, issue['issue_id'], execution_id=run['execution_id'], evidence_ids=evidence_ids, resolved=True)
            else:
                service.confirm_tool_repair(actor, issue['issue_id'], execution_id=run['execution_id'], resolved=True)
        service.store.db.set_trace_callback(None)
        assert changed == [True]
        row = service.store.db.execute('SELECT status,repair_version FROM tool_issues WHERE issue_id=?', (issue['issue_id'],)).fetchone()
        assert row['repair_version'] == 3
        assert row['status'] != 'resolved'
    finally:
        service.store.db.set_trace_callback(None)
        if other is not None:
            other.close()
        service.close()

@pytest.mark.parametrize('operation', ['confirm_repair', 'review_security_issue'])
@pytest.mark.parametrize('resolved', ['false', 'true', 0, 1, None, [], {}])
def test_repair_review_rejects_non_boolean_resolution(tmp_path, operation, resolved):
    service = make_service(tmp_path)
    try:
        details = {'execution_id': 'not-executed', 'resolved': resolved}
        if operation == 'review_security_issue':
            details['evidence_ids'] = ['not-recorded']
        with pytest.raises(ValueError, match='tool_repair_resolution_must_be_boolean'):
            getattr(service.tools, operation)('m1', 'missing-issue', **details)
        assert not service.store.db.in_transaction
    finally:
        service.close()

@pytest.mark.parametrize('output,expected_status', [('ok', 'success'), ('wrong', 'failed')])
def test_configured_executor_uses_business_admission_and_assertions(tmp_path, monkeypatch, output, expected_status):
    from types import SimpleNamespace
    from butian.runtime.execution_contract import CapabilityEvidence, ExecutionCapabilities, ExecutionObservation, REQUIRED_CONTROL
    from butian.runtime.execution_permit import request_digest
    service = make_service(tmp_path)
    caps = ExecutionCapabilities('owner', 'instance', {name: CapabilityEvidence('supported', 'self_test', 1, 'instance') for name in REQUIRED_CONTROL})
    calls = []
    class Runner:
        adapter = SimpleNamespace(capabilities=lambda: caps)
        def run(self, request, *, authorization_valid):
            assert not service.store.db.in_transaction
            assert authorization_valid()
            assert request_digest(request) == request.request_sha256
            assert request.permit['authorization_id'].startswith('business:')
            assert request.permit['member_id'] == 'm1'
            assert service.store.db.execute('SELECT status FROM tool_executions WHERE execution_id=?', (request.execution_id,)).fetchone()[0] == 'running'
            calls.append(request)
            return ExecutionObservation(request.execution_id, 'owner', 'instance', 2, 'success', exit_code=0, tree_exit_confirmed=True, metadata={'stdout': output, 'stderr': ''})
        def unresolved(self):
            return []
        def wait(self, timeout):
            return True
    try:
        tool_id = propose(service)['tool_id']
        manifest = {**MANIFEST, 'permissions': ['execute'], 'input_contract': {'type': 'object'}, 'business_assertions': [{'path': '$', 'expected': 'ok'}], 'isolation': {'profile': 'low_risk_host', 'requires': ['process_tree_control']}}
        service.add_tool_version('m1', tool_id, "print('never locally launched')", manifest)
        service.tools.execution_runner = Runner()
        def forbidden_spawn(*args, **kwargs):
            raise AssertionError('configured executor must not fall back to Popen')
        monkeypatch.setattr('butian.runtime.tools.subprocess.Popen', forbidden_spawn)
        result = execute_in_task(service, 'm1', tool_id, 1, {}, verification_only=True)
        assert len(calls) == 1
        assert result['runtime_backend'] == 'unified_executor'
        assert result['status'] == expected_status
        assert result['audit_status'] == 'committed'
        assert result['isolation_capabilities']['tree_exit_confirmed'] is True
    finally:
        service.close()


def test_unified_host_revocation_is_committed_before_remote_call(tmp_path):
    from types import SimpleNamespace
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, 'print(1)', MANIFEST)
        grant = service.grant_host_authorization('m1', tool_id, 1,
                    capabilities=['process.spawn'], reason='test')
        calls = []
        class Adapter:
            def capabilities(self): return SimpleNamespace(executor_id='executor', instance_id='original')
            def revoke(self, identity, generation):
                assert not service.store.db.in_transaction
                row = service.store.db.execute('SELECT status FROM host_authorizations WHERE authorization_id=?', (identity,)).fetchone()
                assert row['status'] == 'revoked'
                calls.append((identity, generation))
                return {'authorization_id': identity, 'executions': []}
        service.tools.execution_runner = SimpleNamespace(adapter=Adapter())
        service.tools._bind_host_executor(grant['authorization_id'], service.tools.execution_runner.adapter.capabilities())
        service.revoke_host_authorization('m1', grant['authorization_id'], reason='revoke')
        assert calls == [(grant['authorization_id'], 0)]
    finally:
        service.tools.execution_runner = None
        service.close()


def test_host_revocation_audit_failure_rolls_back(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, 'print(1)', MANIFEST)
        grant = service.grant_host_authorization('m1', tool_id, 1,
                    capabilities=['process.spawn'], reason='test')
        def fail_event(*args, **kwargs):
            raise RuntimeError('audit unavailable')
        monkeypatch.setattr(service.tools, '_event', fail_event)
        with pytest.raises(RuntimeError, match='audit unavailable'):
            service.revoke_host_authorization('m1', grant['authorization_id'], reason='revoke')
        row = service.store.db.execute('SELECT status FROM host_authorizations WHERE authorization_id=?', (grant['authorization_id'],)).fetchone()
        assert row['status'] == 'active'
    finally:
        service.close()


def test_host_revocation_lost_ack_is_durable_and_retryable(tmp_path):
    from types import SimpleNamespace
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, 'print(1)', MANIFEST)
        grant = service.grant_host_authorization('m1', tool_id, 1,
                    capabilities=['process.spawn'], reason='test')
        class Adapter:
            def capabilities(self): return SimpleNamespace(executor_id='executor', instance_id='original')
            def revoke(self, identity, generation):
                raise OSError('ack lost')
        service.tools.execution_runner = SimpleNamespace(adapter=Adapter())
        service.tools._bind_host_executor(grant['authorization_id'], service.tools.execution_runner.adapter.capabilities())
        result = service.revoke_host_authorization('m1', grant['authorization_id'], reason='revoke')
        assert result['executor_revocation']['state'] == 'unknown'
        pending = service.store.db.execute('SELECT delivery_state FROM host_revocation_outbox WHERE authorization_id=?', (grant['authorization_id'],)).fetchone()
        assert pending['delivery_state'] == 'pending'
        service.tools.execution_runner = None
        service.close()
        service = ButianService(tmp_path)
        calls = []
        class Reconnected:
            def capabilities(self): return SimpleNamespace(executor_id='executor', instance_id='original')
            def revoke(self, identity, generation):
                assert not service.store.db.in_transaction
                calls.append((identity, generation))
                return {'authorization_id': identity, 'executions': []}
        service.tools.execution_runner = SimpleNamespace(adapter=Reconnected())
        assert service.tools.retry_host_revocations() == 1
        assert calls == [(grant['authorization_id'], 0)]
        assert service.tools.retry_host_revocations() == 0
    finally:
        service.tools.execution_runner = None
        service.close()


def test_legacy_revoked_authorization_is_backfilled_to_outbox(tmp_path):
    from butian.runtime.service import ButianService
    service = make_service(tmp_path)
    tool = propose(service)['tool_id']
    service.add_tool_version('m1', tool, "print('ok')", {**MANIFEST, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py']})
    authorization = service.tools.grant_host_authorization('m1', tool, 1, capabilities=['process.spawn'], reason='test')
    identity = authorization['authorization_id']
    service.store.db.execute("UPDATE host_authorizations SET status='revoked',revoked_at=1 WHERE authorization_id=?", (identity,))
    service.close()
    service = ButianService(tmp_path)
    try:
        row = service.store.db.execute('SELECT delivery_state FROM host_revocation_outbox WHERE authorization_id=?', (identity,)).fetchone()
        assert row is not None
        assert row[0] == 'pending'
    finally:
        service.close()


def test_tool_wait_checks_configured_executor_exit_confirmation(tmp_path):
    from types import SimpleNamespace
    service = make_service(tmp_path)
    calls = []
    confirmed = False
    def wait(timeout):
        calls.append(timeout)
        return confirmed
    service.tools.execution_runner = SimpleNamespace(wait=wait)
    try:
        assert service.tools.wait_for_executions(timeout=0.02) is False
        confirmed = True
        assert service.tools.wait_for_executions(timeout=0.1) is True
        assert len(calls) == 2
        assert 0 <= calls[0] <= 0.02
        assert 0 <= calls[1] <= 0.1
    finally:
        service.tools.execution_runner = None
        service.close()


@pytest.mark.parametrize('receipt', [None, {}, {'authorization_id': 'other', 'executions': []}])
def test_host_revocation_wrong_receipt_remains_pending(tmp_path, receipt):
    from types import SimpleNamespace
    service = make_service(tmp_path)
    try:
        tool = propose(service)['tool_id']
        service.add_tool_version('m1', tool, 'print(1)', MANIFEST)
        grant = service.grant_host_authorization('m1', tool, 1,
                    capabilities=['process.spawn'], reason='test')
        service.tools.execution_runner = SimpleNamespace(adapter=SimpleNamespace(
            capabilities=lambda: SimpleNamespace(executor_id='executor', instance_id='original'), revoke=lambda *args: receipt))
        service.tools._bind_host_executor(grant['authorization_id'], service.tools.execution_runner.adapter.capabilities())
        result = service.revoke_host_authorization('m1', grant['authorization_id'], reason='revoke')
        assert result['executor_revocation']['state'] == 'unknown'
        row = service.store.db.execute('SELECT delivery_state FROM host_revocation_outbox WHERE authorization_id=?', (grant['authorization_id'],)).fetchone()
        assert row['delivery_state'] == 'pending'
    finally:
        service.tools.execution_runner = None
        service.close()


def test_host_revocation_retry_refuses_different_executor_instance(tmp_path):
    from types import SimpleNamespace
    service = make_service(tmp_path)
    calls = []
    identity = ['first']
    def capabilities():
        return SimpleNamespace(executor_id='executor', instance_id=identity[0])
    def revoke(auth, generation):
        calls.append(identity[0])
        raise OSError('lost acknowledgement')
    try:
        tool = propose(service)['tool_id']
        service.add_tool_version('m1', tool, 'print(1)', MANIFEST)
        grant = service.grant_host_authorization('m1', tool, 1, capabilities=['process.spawn'], reason='test')
        service.tools.execution_runner = SimpleNamespace(adapter=SimpleNamespace(capabilities=capabilities, revoke=revoke))
        service.tools._bind_host_executor(grant['authorization_id'], service.tools.execution_runner.adapter.capabilities())
        service.revoke_host_authorization('m1', grant['authorization_id'], reason='revoke')
        identity[0] = 'replacement'
        assert service.tools.retry_host_revocations() == 0
        assert calls == ['first']
    finally:
        service.tools.execution_runner = None
        service.close()


def test_concurrent_revocation_receipts_commit_one_audit(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    service = make_service(tmp_path)
    barrier = threading.Barrier(2)
    try:
        tool = propose(service)['tool_id']
        service.add_tool_version('m1', tool, 'print(1)', MANIFEST)
        grant = service.grant_host_authorization('m1', tool, 1, capabilities=['process.spawn'], reason='test')
        auth = grant['authorization_id']
        service.store.db.execute("UPDATE host_authorizations SET status='revoked' WHERE authorization_id=?", (auth,))
        service.store.db.execute("INSERT INTO host_revocation_outbox (authorization_id,executor_id,instance_id,delivery_state,created_at) VALUES (?,'executor','original','pending',1)", (auth,))
        def revoke(identity, generation):
            barrier.wait(timeout=3)
            return {'authorization_id': identity, 'executions': []}
        service.tools.execution_runner = SimpleNamespace(adapter=SimpleNamespace(
            capabilities=lambda: SimpleNamespace(executor_id='executor', instance_id='original'), revoke=revoke))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(service.tools._deliver_host_revocation, auth) for _ in range(2)]
            assert [f.result()['state'] for f in futures] == ['acknowledged', 'acknowledged']
        row = service.store.db.execute('SELECT attempts,delivery_state FROM host_revocation_outbox WHERE authorization_id=?', (auth,)).fetchone()
        assert row['attempts'] == 1
        assert row['delivery_state'] == 'acknowledged'
    finally:
        service.tools.execution_runner = None
        service.close()

def test_host_revocation_first_delivery_keeps_granted_executor(tmp_path):
    from types import SimpleNamespace
    service = make_service(tmp_path)
    identity = ['original']
    calls = []
    service.tools.execution_runner = SimpleNamespace(adapter=SimpleNamespace(
        capabilities=lambda: SimpleNamespace(executor_id='executor', instance_id=identity[0]),
        revoke=lambda auth, generation: calls.append(identity[0]) or {'authorization_id': auth, 'executions': []}))
    try:
        tool = propose(service)['tool_id']
        service.add_tool_version('m1', tool, 'print(1)', MANIFEST)
        grant = service.grant_host_authorization('m1', tool, 1, capabilities=['process.spawn'], reason='test')
        identity[0] = 'replacement'
        result = service.revoke_host_authorization('m1', grant['authorization_id'], reason='test')
        assert result['executor_revocation']['state'] == 'unknown'
        assert calls == []
        row = service.store.db.execute('SELECT executor_id,instance_id,delivery_state FROM host_revocation_outbox WHERE authorization_id=?', (grant['authorization_id'],)).fetchone()
        assert tuple(row) == ('executor', 'original', 'pending')
    finally:
        service.tools.execution_runner = None
        service.close()


def test_host_revocation_unbound_legacy_does_not_guess_current_owner(tmp_path):
    from types import SimpleNamespace
    service = make_service(tmp_path)
    service.tools.execution_runner = None
    calls = []
    try:
        tool = propose(service)['tool_id']
        service.add_tool_version('m1', tool, 'print(1)', MANIFEST)
        grant = service.grant_host_authorization('m1', tool, 1, capabilities=['process.spawn'], reason='test')
        service.tools.execution_runner = SimpleNamespace(adapter=SimpleNamespace(
            capabilities=lambda: SimpleNamespace(executor_id='executor', instance_id='replacement'),
            revoke=lambda *args: calls.append(args) or {'authorization_id': grant['authorization_id'], 'executions': []}))
        result = service.revoke_host_authorization('m1', grant['authorization_id'], reason='test')
        assert result['executor_revocation']['state'] == 'unknown'
        assert calls == []
        row = service.store.db.execute('SELECT executor_id,instance_id,delivery_state FROM host_revocation_outbox WHERE authorization_id=?', (grant['authorization_id'],)).fetchone()
        assert tuple(row) == (None, None, 'pending')
    finally:
        service.tools.execution_runner = None
        service.close()
