from __future__ import annotations

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
        manifest=MANIFEST,
    )


def test_tool_proposal_gate_and_numeric_validation(tmp_path):
    service = make_service(tmp_path)
    try:
        rejected = service.propose_tool("m1", "小工具", "低价值", reuse_lower=1, manual_cost_upper=1, call_cost_lower=1, build_cost_upper=1, maintenance_cost_upper=0, risk_cost_upper=0)
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
        version = service.add_tool_version("m1", tool_id, "return input", MANIFEST)
        assert version["version"] == 1
        row = service.tools._tool(tool_id)
        source = service.store.db.execute("SELECT source_path FROM tool_versions WHERE tool_id=? AND version=1", (tool_id,)).fetchone()[0]
        assert f"members{__import__('os').sep}m1{__import__('os').sep}tools" in source
        with pytest.raises(ValueError, match="access_denied"):
            service.add_tool_version("m2", tool_id, "bad", MANIFEST)
        first = service.verify_tool("m1", tool_id, 1, "success", independent_key="case-a", environment={"kind": "unit"})
        assert first["status"] == "candidate"
        second = service.verify_tool("m1", tool_id, 1, "success", independent_key="case-b", environment={"kind": "integration"})
        assert second["status"] == "active"
        duplicate = service.verify_tool("m1", tool_id, 1, "success", independent_key="case-a")
        assert duplicate["verification_id"] == first["verification_id"]
        assert service.tool_stats(tool_id, 1)["successes"] == 2
    finally:
        service.close()


def test_only_active_tool_can_share_and_share_is_pending(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)["tool_id"]
        service.add_tool_version("m1", tool_id, "tool", MANIFEST)
        with pytest.raises(ValueError, match="only_active"):
            service.share_tool("m1", tool_id, "m2")
        service.verify_tool("m1", tool_id, 1, "success", independent_key="s1")
        service.verify_tool("m1", tool_id, 1, "success", independent_key="s2")
        shared = service.share_tool("m1", tool_id, "m2", permission="team_execute")
        assert shared["status"] == "shared_candidate"
        assert shared["permission"] == "team_execute"
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
        service.verify_tool("m1", tool_id, 1, "success", independent_key="ctx-a")
        service.verify_tool("m1", tool_id, 1, "success", independent_key="ctx-b")
        composed, report = service.compose_context("m1", "请分析输入", include_tools=True, budgets={"TOOLS": 20})
        assert "工具卡片" not in composed
        assert "SECRET_SOURCE_BYTES" not in composed
        assert report.tools_included == 0
        assert report.tools_omitted == 1
        composed, report = service.compose_context("m1", "请分析输入", include_tools=True)
        assert "工具卡片" in composed
        assert "通用分析" in composed
        assert "SECRET_SOURCE_BYTES" not in composed
        assert report.tools_included == 1
    finally:
        service.close()


def test_controlled_execution_dry_run_audit_and_limits(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "permissions": ["cpu", "execute"], "entrypoint": [sys.executable, "-c", "import sys; print(sys.stdin.read())"]}
        tool_id = service.propose_tool("m1", "执行分析", "受控执行分析", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1, manifest=manifest)["tool_id"]
        service.add_tool_version("m1", tool_id, "unused", manifest)
        service.verify_tool("m1", tool_id, 1, "success", independent_key="exec-a")
        service.verify_tool("m1", tool_id, 1, "success", independent_key="exec-b")
        preview = service.execute_tool("m1", tool_id, 1, {"x": 1}, dry_run=True)
        assert preview["status"] == "dry_run"
        result = service.execute_tool("m1", tool_id, 1, {"x": 1})
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
        service.verify_tool("m1", tool_id, 1, "success", independent_key="supply-a")
        result = service.verify_tool("m1", tool_id, 1, "success", independent_key="supply-b")
        assert result["status"] == "active"
    finally:
        service.close()

def test_execution_failures_create_repair_issues_without_discarding_asset(tmp_path):
    service = make_service(tmp_path)
    try:
        manifest = {**MANIFEST, "permissions": ["execute"], "entrypoint": [sys.executable, "-c", "import sys; sys.exit(3)"]}
        tool_id = service.propose_tool("m1", "失败工具", "验证熔断", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1, manifest=manifest)["tool_id"]
        service.add_tool_version("m1", tool_id, "unused", manifest)
        service.verify_tool("m1", tool_id, 1, "success", independent_key="cb-a")
        service.verify_tool("m1", tool_id, 1, "success", independent_key="cb-b")
        for _ in range(3):
            result = service.execute_tool("m1", tool_id, 1, {})
            assert result["status"] == "failed"
        assert service.list_tools("m1")[0]["status"] == "active"
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
        service.verify_tool("m1", tool_id, 1, "success", independent_key="share-a")
        service.verify_tool("m1", tool_id, 1, "success", independent_key="share-b")
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
        manifest = {**MANIFEST, "permissions": ["execute"], "entrypoint": [sys.executable, "-c", "print('ok')"]}
        service.add_tool_version("m1", tool_id, "v1", manifest)
        for key in ("repair-a", "repair-b"):
            service.verify_tool("m1", tool_id, 1, "success", independent_key=key)
        issue = service.report_tool_issue("m1", tool_id, 1, description="Wrong boundary handling")
        service.add_tool_version("m1", tool_id, "v2", manifest)
        assert service.tools._tool(tool_id)["current_version"] == 1
        service.repair_tool_issue("m1", issue["issue_id"], repair_version=2, diagnosis="Correct boundary")
        dry = service.execute_tool("m1", tool_id, 2, dry_run=True)
        with pytest.raises(ValueError, match="execution_mismatch"):
            service.confirm_tool_repair("m1", issue["issue_id"], execution_id=dry["execution_id"], resolved=True)
        run = service.execute_tool("m1", tool_id, 2)
        assert service.confirm_tool_repair("m1", issue["issue_id"], execution_id=run["execution_id"], resolved=True)["status"] == "resolved"
        assert service.tools._tool(tool_id)["current_version"] == 1
        with pytest.raises(ValueError, match="access_denied"):
            service.repair_tool_issue("m2", issue["issue_id"], repair_version=2, diagnosis="unauthorized")
    finally:
        service.close()

def test_tool_integrity_and_personas(tmp_path):
    service = ButianService(tmp_path)
    try:
        created = service.bootstrap_personas()
        assert len(created) == 6
        rows = service.store.db.execute("SELECT name, responsibilities FROM members ORDER BY member_id").fetchall()
        assert {row["name"] for row in rows} == {"许伟", "沈衡", "陆徵", "宋析", "苏衍", "何溪"}
        assert all("特质:" in row["responsibilities"] and "领域:" in row["responsibilities"] for row in rows)
        assert all((tmp_path / "members" / mid / "memory.db").is_file() for mid in created)
        service.add_member("owner", "作者")
        manifest = {"input_contract": {"type": "object"}, "output_contract": {"type": "object"}, "permissions": ["execute"], "failure_conditions": ["bad"], "entrypoint": [sys.executable, "-c", "print('not-json')"]}
        tid = service.propose_tool("owner", "contract", "contract", reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=1, maintenance_cost_upper=1, risk_cost_upper=1, manifest=manifest)["tool_id"]
        service.add_tool_version("owner", tid, "source", manifest)
        service.verify_tool("owner", tid, 1, "success", independent_key="i1")
        service.verify_tool("owner", tid, 1, "success", independent_key="i2")
        result = service.execute_tool("owner", tid, 1, {})
        assert result["status"] == "failed"
    finally:
        service.close()
