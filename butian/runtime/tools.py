from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


STATUSES = {"draft", "candidate", "active", "deprecated", "quarantined", "archived", "shared_candidate"}
TERMINAL_BAD = {"deprecated", "quarantined", "archived"}
_ALLOWED_TRANSITIONS = {
    "draft": {"candidate", "active", "archived"},
    "candidate": {"active", "archived"},
    "active": {"archived", "deprecated", "quarantined"},
    "deprecated": {"archived"},
    "quarantined": {"archived", "candidate"},
    "archived": {"draft", "candidate"},
}


def _finite(value: Any, name: str, *, nonnegative: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}_must_be_finite")
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValueError(f"{name}_must_be_finite")
    return result


def wilson_lower(successes: int, failures: int, z: float = 1.96) -> float:
    if successes < 0 or failures < 0 or not math.isfinite(z) or z <= 0:
        raise ValueError("invalid_reliability_inputs")
    n = successes + failures
    if n == 0:
        return 0.0
    p = successes / n
    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / denominator


class ToolStore:
    """Member-owned tool assets; source files never enter provider context by default."""

    def __init__(self, store, home: str | Path):
        self.store = store
        self.home = Path(home)
        self.tools_root = self.home / "members"
        self._create_schema()

    def _create_schema(self):
        self.store.db.executescript("""
        CREATE TABLE IF NOT EXISTS tool_assets (
            tool_id TEXT PRIMARY KEY, owner_member_id TEXT NOT NULL, name TEXT NOT NULL,
            description TEXT NOT NULL, status TEXT NOT NULL, manifest TEXT NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, current_version INTEGER NOT NULL DEFAULT 0,
            UNIQUE(owner_member_id, name)
        );
        CREATE TABLE IF NOT EXISTS tool_versions (
            tool_id TEXT NOT NULL, version INTEGER NOT NULL, content_hash TEXT NOT NULL,
            source_path TEXT NOT NULL, manifest TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(tool_id, version)
        );
        CREATE TABLE IF NOT EXISTS tool_events (
            event_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL, version INTEGER,
            event_type TEXT NOT NULL, payload TEXT NOT NULL, idempotency_key TEXT UNIQUE, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tool_verifications (
            verification_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL, version INTEGER NOT NULL,
            task_id TEXT, environment TEXT NOT NULL, outcome TEXT NOT NULL, evidence_ids TEXT NOT NULL,
            independent_key TEXT NOT NULL UNIQUE, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tool_shares (
            share_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL, version INTEGER NOT NULL,
            owner_member_id TEXT NOT NULL, recipient_member_id TEXT NOT NULL, permission TEXT NOT NULL,
            status TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(tool_id, version, recipient_member_id)
        );
        CREATE TABLE IF NOT EXISTS tool_issues (
            issue_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL, version INTEGER NOT NULL,
            reporter_member_id TEXT NOT NULL, execution_id TEXT, description TEXT NOT NULL,
            severity TEXT NOT NULL, status TEXT NOT NULL, repair_version INTEGER,
            diagnosis TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tools_owner_status ON tool_assets(owner_member_id, status);
        CREATE INDEX IF NOT EXISTS idx_tool_verify ON tool_verifications(tool_id, version, outcome);
        CREATE TABLE IF NOT EXISTS tool_executions (
            execution_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL, version INTEGER NOT NULL,
            actor_member_id TEXT NOT NULL, task_id TEXT, input_hash TEXT NOT NULL,
            dry_run INTEGER NOT NULL, status TEXT NOT NULL, exit_code INTEGER,
            stdout TEXT NOT NULL, stderr TEXT NOT NULL, termination_reason TEXT,
            permissions TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL
        );
        """)

    def execute(self, actor_member_id: str, tool_id: str, version: int, input_data: Any = None, *, dry_run: bool = False, task_id: str | None = None, timeout_seconds: float = 10.0, max_output_bytes: int = 65536, share_id: str | None = None) -> dict[str, Any]:
        """Execute one immutable active version through a manifest-declared argv only."""
        tool = self._tool(tool_id)
        access = actor_member_id == tool["owner_member_id"]
        permission = "owner"
        if not access and share_id:
            share = self.store.db.execute("SELECT * FROM tool_shares WHERE share_id=? AND tool_id=? AND recipient_member_id=?", (share_id, tool_id, actor_member_id)).fetchone()
            if share and share["status"] == "active" and share["permission"] == "team_execute" and share["version"] == version:
                access, permission = True, share["permission"]
        if not access:
            raise ValueError("tool_execute_denied")
        if tool["status"] != "active":
            raise ValueError("only_active_tool_can_execute")
        if version != tool["current_version"] and actor_member_id != tool["owner_member_id"]:
            repair_share = self.store.db.execute("SELECT permission, status FROM tool_shares WHERE tool_id=? AND version=? AND recipient_member_id=?", (tool_id, version, actor_member_id)).fetchone()
            if not repair_share or repair_share["status"] != "active" or repair_share["permission"] != "team_execute":
                raise ValueError("repair_version_requires_owner_trial")
        row = self.store.db.execute("SELECT * FROM tool_versions WHERE tool_id=? AND version=?", (tool_id, version)).fetchone()
        if not row:
            raise ValueError("tool_version_not_found")
        manifest = json.loads(row["manifest"])
        source = Path(row["source_path"])
        if not source.is_file():
            raise ValueError("tool_source_not_found")
        actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual_hash != row["content_hash"] or manifest.get("content_hash") != actual_hash:
            raise ValueError("tool_content_hash_mismatch")
        permissions = manifest.get("permissions", [])
        if "execute" not in permissions:
            raise ValueError("tool_execute_permission_missing")
        argv = manifest.get("entrypoint")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("tool_entrypoint_must_be_argv")
        if any("{input}" in item or "{source}" in item for item in argv):
            raise ValueError("tool_entrypoint_placeholders_forbidden")
        timeout = _finite(timeout_seconds, "timeout_seconds", nonnegative=True)
        output_limit = int(_finite(max_output_bytes, "max_output_bytes", nonnegative=True))
        if timeout <= 0 or timeout > 300 or output_limit <= 0 or output_limit > 4 * 1024 * 1024:
            raise ValueError("tool_resource_limit_invalid")
        encoded = json.dumps(input_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        input_hash = hashlib.sha256(encoded).hexdigest()
        execution_id = f"exec_{uuid.uuid4().hex}"; started = time.time()
        common = {"execution_id": execution_id, "tool_id": tool_id, "version": version, "actor_member_id": actor_member_id, "task_id": task_id, "input_hash": input_hash, "permissions": permissions}
        if dry_run:
            self.store.db.execute("INSERT INTO tool_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (execution_id, tool_id, version, actor_member_id, task_id, input_hash, 1, "dry_run", None, "", "", "dry_run", json.dumps(permissions), started, started))
            self._event(tool_id, version, "execution_dry_run", {"execution_id": execution_id, "actor_member_id": actor_member_id, "argv": argv, "permissions": permissions}, f"tool:execution:{execution_id}")
            return {**common, "status": "dry_run", "argv": argv, "permissions": permissions}
        if "network" in permissions:
            raise ValueError("tool_network_permission_not_supported")
        result_status, exit_code, stdout, stderr, reason = "failed", None, "", "", None
        try:
            with tempfile.TemporaryDirectory(prefix="butian-tool-") as work:
                proc = subprocess.run(argv, input=encoded, capture_output=True, cwd=work, env={"PATH": os.environ.get("PATH", "")}, shell=False, timeout=timeout, check=False)
                exit_code = proc.returncode
                stdout = proc.stdout[:output_limit].decode("utf-8", "replace")
                stderr = proc.stderr[:output_limit].decode("utf-8", "replace")
                result_status = "success" if exit_code == 0 else "failed"
                reason = None if exit_code == 0 else "nonzero_exit"
                contract = manifest.get("output_contract", {})
                if result_status == "success" and contract.get("type") == "object":
                    try:
                        decoded = json.loads(stdout)
                    except (TypeError, ValueError):
                        result_status, reason = "failed", "output_contract_invalid_json"
                    else:
                        if not isinstance(decoded, dict):
                            result_status, reason = "failed", "output_contract_type_mismatch"
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or b"")[:output_limit].decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")[:output_limit]
            stderr = (exc.stderr or b"")[:output_limit].decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")[:output_limit]
            result_status, reason = "cancelled", "timeout"
        except OSError as exc:
            stderr, reason = str(exc), "launch_failed"
        finished = time.time()
        self.store.db.execute("INSERT INTO tool_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (execution_id, tool_id, version, actor_member_id, task_id, input_hash, 0, result_status, exit_code, stdout, stderr, reason, json.dumps(permissions), started, finished))
        self._event(tool_id, version, "execution", {"execution_id": execution_id, "actor_member_id": actor_member_id, "status": result_status, "reason": reason}, f"tool:execution:{execution_id}")
        if result_status in {"failed", "cancelled"}:
            self.report_issue(actor_member_id, tool_id, version,
                              description=f"Execution {execution_id}: {reason}",
                              execution_id=execution_id)
        return {**common, "status": result_status, "exit_code": exit_code, "stdout": stdout, "stderr": stderr, "termination_reason": reason}

    def _issue_access(self, actor, tool_id, version):
        tool = self._tool(tool_id)
        self._member_dir(actor)
        if not self.store.db.execute("SELECT 1 FROM tool_versions WHERE tool_id=? AND version=?", (tool_id, version)).fetchone():
            raise ValueError("tool_version_not_found")
        if actor != tool["owner_member_id"] and not self.store.db.execute(
            "SELECT 1 FROM tool_shares WHERE tool_id=? AND version=? AND recipient_member_id=?", (tool_id, version, actor)
        ).fetchone():
            raise ValueError("tool_access_denied")
        return tool

    def report_issue(self, actor, tool_id, version, *, description, execution_id=None, severity="bug"):
        self._issue_access(actor, tool_id, version)
        if severity not in {"bug", "environment", "improvement", "security"} or not description.strip():
            raise ValueError("invalid_tool_issue")
        if execution_id:
            row = self.store.db.execute("SELECT * FROM tool_executions WHERE execution_id=?", (execution_id,)).fetchone()
            if not row or (row["tool_id"], row["version"], row["actor_member_id"]) != (tool_id, version, actor):
                raise ValueError("tool_issue_execution_mismatch")
            old = self.store.db.execute("SELECT * FROM tool_issues WHERE execution_id=?", (execution_id,)).fetchone()
            if old:
                return dict(old)
        issue_id = "issue_" + uuid.uuid4().hex
        now = time.time()
        self.store.db.execute("INSERT INTO tool_issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (issue_id, tool_id, version, actor, execution_id, description, severity, "open", None, None, now, now))
        self._event(tool_id, version, "issue_reported", {"issue_id": issue_id, "reporter": actor, "severity": severity}, "issue:" + issue_id)
        return dict(self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?", (issue_id,)).fetchone())

    def list_issues(self, actor, tool_id):
        tool = self._tool(tool_id)
        self._member_dir(actor)
        if actor == tool["owner_member_id"]:
            rows = self.store.db.execute("SELECT * FROM tool_issues WHERE tool_id=? ORDER BY created_at", (tool_id,))
        else:
            rows = self.store.db.execute("SELECT * FROM tool_issues WHERE tool_id=? AND reporter_member_id=? ORDER BY created_at", (tool_id, actor))
        return [dict(row) for row in rows]

    def repair_issue(self, actor, issue_id, *, repair_version, diagnosis):
        issue = self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?", (issue_id,)).fetchone()
        if not issue or self._tool(issue["tool_id"])["owner_member_id"] != actor:
            raise ValueError("tool_access_denied")
        if issue["status"] != "open" or repair_version <= issue["version"] or not diagnosis.strip():
            raise ValueError("invalid_tool_repair")
        self._issue_access(actor, issue["tool_id"], repair_version)
        self.store.db.execute("UPDATE tool_issues SET status='awaiting_confirmation', repair_version=?, diagnosis=?, updated_at=? WHERE issue_id=?", (repair_version, diagnosis, time.time(), issue_id))
        share_id = f"share_repair_{uuid.uuid4().hex}"
        self.store.db.execute("INSERT OR REPLACE INTO tool_shares VALUES (?,?,?,?,?,?,?,?)", (share_id, issue["tool_id"], repair_version, actor, issue["reporter_member_id"], "team_execute", "active", time.time()))
        self._event(issue["tool_id"], repair_version, "repair_submitted", {"issue_id": issue_id, "diagnosis": diagnosis}, "repair:" + issue_id + ":" + str(repair_version))
        return {"issue_id": issue_id, "status": "awaiting_confirmation"}

    def confirm_repair(self, actor, issue_id, *, execution_id, resolved):
        issue = self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?", (issue_id,)).fetchone()
        if not issue or issue["reporter_member_id"] != actor:
            raise ValueError("tool_access_denied")
        if issue["status"] != "awaiting_confirmation":
            raise ValueError("tool_issue_not_awaiting_confirmation")
        run = self.store.db.execute("SELECT * FROM tool_executions WHERE execution_id=?", (execution_id,)).fetchone()
        if not run or (run["tool_id"], run["version"], run["actor_member_id"]) != (issue["tool_id"], issue["repair_version"], actor) or run["dry_run"]:
            raise ValueError("tool_repair_execution_mismatch")
        if not isinstance(resolved, bool) or (resolved and run["status"] != "success"):
            raise ValueError("tool_repair_not_successful")
        status = "resolved" if resolved else "open"
        self.store.db.execute("UPDATE tool_issues SET status=?, updated_at=? WHERE issue_id=?", (status, time.time(), issue_id))
        self._event(issue["tool_id"], issue["repair_version"], "repair_confirmed", {"issue_id": issue_id, "resolved": resolved, "execution_id": execution_id}, "confirmation:" + issue_id + ":" + execution_id)
        return {"issue_id": issue_id, "status": status}

    def list_executions(self, actor_member_id: str, *, tool_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("invalid_execution_limit")
        sql = "SELECT * FROM tool_executions WHERE actor_member_id=?"; args: list[Any] = [actor_member_id]
        if tool_id:
            sql += " AND tool_id=?"; args.append(tool_id)
        sql += " ORDER BY started_at DESC LIMIT ?"; args.append(limit)
        return [dict(row) for row in self.store.db.execute(sql, args)]

    def _member_dir(self, member_id: str) -> Path:
        if not self.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
            raise ValueError("member_not_found")
        path = (self.tools_root / member_id / "tools").resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _tool(self, tool_id: str) -> dict[str, Any]:
        row = self.store.db.execute("SELECT * FROM tool_assets WHERE tool_id=?", (tool_id,)).fetchone()
        if not row:
            raise ValueError("tool_not_found")
        item = dict(row)
        item["manifest"] = json.loads(item["manifest"])
        return item

    def propose(self, owner_member_id: str, name: str, description: str, *, reuse_lower: float, manual_cost_upper: float, call_cost_lower: float, build_cost_upper: float, maintenance_cost_upper: float, risk_cost_upper: float, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
        self._member_dir(owner_member_id)
        values = {k: _finite(v, k, nonnegative=True) for k, v in {"reuse_lower": reuse_lower, "manual_cost_upper": manual_cost_upper, "call_cost_lower": call_cost_lower, "build_cost_upper": build_cost_upper, "maintenance_cost_upper": maintenance_cost_upper, "risk_cost_upper": risk_cost_upper}.items()}
        # 保守下界：复用下界乘以每次净收益下界；人工收益取下界、调用成本取上界。
        per_call_saving_lower = max(0.0, values["manual_cost_upper"] - values["call_cost_lower"])
        benefit_lower = values["reuse_lower"] * per_call_saving_lower - values["build_cost_upper"] - values["maintenance_cost_upper"] - values["risk_cost_upper"]
        proposal = {**values, "benefit_lower": benefit_lower, "gate": "pass" if benefit_lower > 0 else "reject", "manifest": manifest or {}}
        if benefit_lower <= 0:
            return {"status": "rejected", "reason": "insufficient_reuse_value", "proposal": proposal}
        now = time.time(); tool_id = f"tool_{uuid.uuid4().hex}"
        self.store.db.execute("INSERT INTO tool_assets VALUES (?,?,?,?,?,?,?,?,?)", (tool_id, owner_member_id, name, description, "draft", json.dumps(proposal, ensure_ascii=False), now, now, 0))
        self._event(tool_id, None, "proposed", proposal, f"tool:{tool_id}:proposed")
        return {"status": "draft", "tool_id": tool_id, "proposal": proposal}

    def add_version(self, owner_member_id: str, tool_id: str, content: bytes | str, manifest: dict[str, Any]) -> dict[str, Any]:
        tool = self._tool(tool_id)
        if tool["owner_member_id"] != owner_member_id:
            raise ValueError("tool_access_denied")
        required = ("input_contract", "output_contract", "permissions", "failure_conditions")
        if any(key not in manifest for key in required):
            raise ValueError("tool_manifest_incomplete")
        source_kind = manifest.get("source_kind", "internal")
        if source_kind not in {"internal", "external", "imported", "shared"}:
            raise ValueError("invalid_tool_source_kind")
        if source_kind != "internal" and (not manifest.get("source") or not manifest.get("license") or manifest.get("scan_status") != "passed"):
            raise ValueError("tool_supply_chain_unverified")
        if not isinstance(manifest["permissions"], list) or any(not isinstance(item, str) for item in manifest["permissions"]):
            raise ValueError("tool_permissions_must_be_list")
        if not isinstance(manifest["failure_conditions"], list) or not manifest["failure_conditions"]:
            raise ValueError("tool_failure_conditions_required")
        for contract in (manifest["input_contract"], manifest["output_contract"]):
            if not isinstance(contract, dict) or not contract:
                raise ValueError("tool_contract_must_be_object")
        raw = content.encode() if isinstance(content, str) else content
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            raise ValueError("tool_content_required")
        digest = hashlib.sha256(raw).hexdigest()
        duplicate = self.store.db.execute("SELECT tool_id, version FROM tool_versions WHERE tool_id=? AND content_hash=?", (tool_id, digest)).fetchone()
        if duplicate:
            return {"tool_id": tool_id, "version": duplicate["version"], "content_hash": digest, "status": "idempotent"}
        version = self.store.db.execute("SELECT COALESCE(MAX(version),0)+1 FROM tool_versions WHERE tool_id=?", (tool_id,)).fetchone()[0]
        path = self._member_dir(owner_member_id) / tool_id / f"v{version}"
        path.mkdir(parents=True, exist_ok=False)
        source = path / "tool.bin"
        source.write_bytes(raw)
        now = time.time()
        manifest = {**manifest, "content_hash": digest}
        self.store.db.execute("INSERT INTO tool_versions VALUES (?,?,?,?,?,?)", (tool_id, version, digest, str(source), json.dumps(manifest, ensure_ascii=False), now))
        if tool["status"] != "active":
            self.store.db.execute("UPDATE tool_assets SET current_version=?, manifest=?, updated_at=? WHERE tool_id=?", (version, json.dumps(manifest, ensure_ascii=False), now, tool_id))
        self._event(tool_id, version, "version_added", {"content_hash": digest}, f"tool:{tool_id}:version:{version}")
        return {"tool_id": tool_id, "version": version, "content_hash": digest, "status": "draft"}

    def verify(self, owner_member_id: str, tool_id: str, version: int, outcome: str, *, independent_key: str, task_id: str | None = None, environment: dict[str, Any] | None = None, evidence_ids: list[str] | None = None) -> dict[str, Any]:
        tool = self._tool(tool_id)
        if tool["owner_member_id"] != owner_member_id:
            raise ValueError("tool_access_denied")
        if outcome not in {"success", "failure", "unknown", "cancelled"}:
            raise ValueError("invalid_tool_outcome")
        if not self.store.db.execute("SELECT 1 FROM tool_versions WHERE tool_id=? AND version=?", (tool_id, version)).fetchone():
            raise ValueError("tool_version_not_found")
        now = time.time(); verification_id = f"tverify_{uuid.uuid4().hex}"
        try:
            self.store.db.execute("INSERT INTO tool_verifications VALUES (?,?,?,?,?,?,?,?,?)", (verification_id, tool_id, version, task_id, json.dumps(environment or {}, ensure_ascii=False), outcome, json.dumps(evidence_ids or []), independent_key, now))
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                row = self.store.db.execute("SELECT * FROM tool_verifications WHERE independent_key=?", (independent_key,)).fetchone()
                return dict(row)
            raise
        stats = self.stats(tool_id, version)
        self._event(tool_id, version, "verification", {"outcome": outcome, "stats": stats}, f"tool:{tool_id}:verification:{independent_key}")
        version_row = self.store.db.execute("SELECT manifest FROM tool_versions WHERE tool_id=? AND version=?", (tool_id, version)).fetchone()
        version_manifest = json.loads(version_row["manifest"]) if version_row else {}
        supply_chain_ok = version_manifest.get("source_kind", "internal") == "internal" or (version_manifest.get("source") and version_manifest.get("license") and version_manifest.get("scan_status") == "passed")
        if stats["successes"] >= 2 and stats["wilson_lower"] > 0 and supply_chain_ok and tool["status"] in {"draft", "candidate"}:
            self._transition(tool_id, "active", {"version": version, "stats": stats}, f"tool:{tool_id}:activate:{version}")
            self.store.db.execute("UPDATE tool_assets SET current_version=?, updated_at=? WHERE tool_id=?", (version, time.time(), tool_id))
        elif tool["status"] == "draft":
            self._transition(tool_id, "candidate", {"version": version}, f"tool:{tool_id}:candidate:{version}")
        return {"verification_id": verification_id, "stats": stats, "status": self._tool(tool_id)["status"]}

    def stats(self, tool_id: str, version: int) -> dict[str, Any]:
        rows = self.store.db.execute("SELECT outcome FROM tool_verifications WHERE tool_id=? AND version=?", (tool_id, version)).fetchall()
        counts = {key: sum(row["outcome"] == key for row in rows) for key in ("success", "failure", "unknown", "cancelled")}
        return {"successes": counts["success"], "failures": counts["failure"], "unknown": counts["unknown"], "cancelled": counts["cancelled"], "wilson_lower": wilson_lower(counts["success"], counts["failure"])}

    def list(self, owner_member_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        self._member_dir(owner_member_id)
        sql = "SELECT * FROM tool_assets WHERE owner_member_id=?"; args: list[Any] = [owner_member_id]
        if status: sql += " AND status=?"; args.append(status)
        return [self._tool(row["tool_id"]) for row in self.store.db.execute(sql, args)]

    def active_cards(self, owner_member_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return metadata-only cards; source bytes never enter context composition."""
        if limit < 0:
            raise ValueError("invalid_tool_limit")
        cards = []
        for tool in self.list(owner_member_id, status="active")[:limit]:
            manifest = tool["manifest"] if isinstance(tool["manifest"], dict) else {}
            cards.append({
                "tool_id": tool["tool_id"],
                "name": tool["name"],
                "description": tool["description"],
                "version": tool["current_version"],
                "input_contract": manifest.get("input_contract"),
                "output_contract": manifest.get("output_contract"),
                "permissions": manifest.get("permissions"),
                "failure_conditions": manifest.get("failure_conditions"),
            })
        return cards

    def shares_for(self, member_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        self._member_dir(member_id)
        sql = "SELECT * FROM tool_shares WHERE recipient_member_id=?"; args: list[Any] = [member_id]
        if status:
            sql += " AND status=?"; args.append(status)
        return [dict(row) for row in self.store.db.execute(sql + " ORDER BY created_at DESC", args)]

    def accept_share(self, recipient_member_id: str, share_id: str) -> dict[str, Any]:
        row = self.store.db.execute("SELECT * FROM tool_shares WHERE share_id=? AND recipient_member_id=?", (share_id, recipient_member_id)).fetchone()
        if not row:
            raise ValueError("tool_share_not_found")
        if row["status"] not in {"shared_candidate", "trial"}:
            raise ValueError("tool_share_not_acceptable")
        self.store.db.execute("UPDATE tool_shares SET status='trial' WHERE share_id=?", (share_id,))
        self._event(row["tool_id"], row["version"], "share_accepted", {"recipient_member_id": recipient_member_id, "share_id": share_id}, f"tool:share:{share_id}:accept")
        return {**dict(row), "status": "trial"}

    def trial(self, recipient_member_id: str, share_id: str, outcome: str, *, independent_key: str, task_id: str | None = None, environment: dict[str, Any] | None = None, evidence_ids: list[str] | None = None) -> dict[str, Any]:
        row = self.store.db.execute("SELECT * FROM tool_shares WHERE share_id=? AND recipient_member_id=?", (share_id, recipient_member_id)).fetchone()
        if not row or row["status"] not in {"trial", "active"}:
            raise ValueError("tool_share_not_in_trial")
        if outcome not in {"success", "failure", "unknown", "cancelled"}:
            raise ValueError("invalid_tool_outcome")
        result = self.verify(recipient_member_id, row["tool_id"], row["version"], outcome, independent_key=independent_key, task_id=task_id, environment=environment, evidence_ids=evidence_ids) if row["owner_member_id"] == recipient_member_id else self._record_trial(row, outcome, independent_key, task_id, environment, evidence_ids)
        next_status = "active" if outcome == "success" else "trial"
        self.store.db.execute("UPDATE tool_shares SET status=? WHERE share_id=? AND status IN ('trial','active')", (next_status, share_id))
        return {"share_id": share_id, "outcome": outcome, "status": next_status, "verification": result}

    def _record_trial(self, share, outcome, independent_key, task_id, environment, evidence_ids):
        now = time.time()
        try:
            self.store.db.execute("INSERT INTO tool_verifications VALUES (?,?,?,?,?,?,?,?,?)", (f"tverify_{uuid.uuid4().hex}", share["tool_id"], share["version"], task_id, json.dumps(environment or {}, ensure_ascii=False), outcome, json.dumps(evidence_ids or []), f"share:{share['share_id']}:{independent_key}", now))
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                row = self.store.db.execute("SELECT * FROM tool_verifications WHERE independent_key=?", (f"share:{share['share_id']}:{independent_key}",)).fetchone()
                return dict(row)
            raise
        return {"outcome": outcome, "independent_key": independent_key}

    def archive(self, owner_member_id: str, tool_id: str, *, reason: str = "maintenance") -> dict[str, Any]:
        tool = self._tool(tool_id)
        if tool["owner_member_id"] != owner_member_id:
            raise ValueError("tool_access_denied")
        if tool["status"] == "archived":
            return tool
        if tool["status"] not in {"draft", "candidate", "active", "deprecated", "quarantined"}:
            raise ValueError("invalid_tool_status_transition")
        self._transition(tool_id, "archived", {"reason": reason}, f"tool:{tool_id}:archive")
        return self._tool(tool_id)

    def restore(self, owner_member_id: str, tool_id: str) -> dict[str, Any]:
        tool = self._tool(tool_id)
        if tool["owner_member_id"] != owner_member_id:
            raise ValueError("tool_access_denied")
        if tool["status"] != "archived":
            raise ValueError("tool_not_archived")
        target = "candidate" if tool["current_version"] else "draft"
        self._transition(tool_id, target, {"restored": True, "requires_reverification": bool(tool["current_version"])}, f"tool:{tool_id}:restore")
        return self._tool(tool_id)

    def share(self, owner_member_id: str, tool_id: str, recipient_member_id: str, *, permission: str = "team_readonly"):
        if permission not in {"team_readonly", "team_execute", "shareable_copy"}: raise ValueError("invalid_tool_share_permission")
        tool = self._tool(tool_id)
        if tool["owner_member_id"] != owner_member_id: raise ValueError("tool_access_denied")
        if tool["status"] != "active": raise ValueError("only_active_tool_can_be_shared")
        self._member_dir(recipient_member_id)
        share_id = f"share_{uuid.uuid4().hex}"
        self.store.db.execute("INSERT OR REPLACE INTO tool_shares VALUES (?,?,?,?,?,?,?,?)", (share_id, tool_id, tool["current_version"], owner_member_id, recipient_member_id, permission, "shared_candidate", time.time()))
        self._event(tool_id, tool["current_version"], "shared", {"recipient_member_id": recipient_member_id, "permission": permission}, f"tool:{tool_id}:share:{recipient_member_id}")
        return {"share_id": share_id, "tool_id": tool_id, "version": tool["current_version"], "status": "shared_candidate", "permission": permission}

    def _transition(self, tool_id: str, status: str, payload: dict[str, Any], key: str):
        if status not in STATUSES:
            raise ValueError("invalid_tool_status")
        current = self.store.db.execute("SELECT status FROM tool_assets WHERE tool_id=?", (tool_id,)).fetchone()
        if not current:
            raise ValueError("tool_not_found")
        if status not in _ALLOWED_TRANSITIONS.get(current["status"], set()):
            raise ValueError("invalid_tool_status_transition")
        self.store.db.execute("UPDATE tool_assets SET status=?, updated_at=? WHERE tool_id=?", (status, time.time(), tool_id))
        self._event(tool_id, None, "status_changed", {"status": status, **payload}, key)

    def _event(self, tool_id: str, version: int | None, event_type: str, payload: dict[str, Any], key: str):
        self.store.db.execute("INSERT OR IGNORE INTO tool_events VALUES (?,?,?,?,?,?,?)", (f"tevent_{uuid.uuid4().hex}", tool_id, version, event_type, json.dumps(payload, ensure_ascii=False), key, time.time()))
