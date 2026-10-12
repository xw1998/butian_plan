from __future__ import annotations



import hashlib

import json

import math

import os

import subprocess

import sys

import threading

import tempfile

import time

import uuid
import shutil

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





def _terminate_process_tree(proc: subprocess.Popen[Any]) -> None:
    """Best-effort cleanup; this is not an isolation boundary."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def _finite(value: Any, name: str, *, nonnegative: bool = False) -> float:

    try:

        result = float(value)

    except (TypeError, ValueError):

        raise ValueError(f"{name}_must_be_finite")

    if not math.isfinite(result) or (nonnegative and result < 0):

        raise ValueError(f"{name}_must_be_finite")

    return result





def _contract_errors(value: Any, contract: dict[str, Any], path: str = "$", depth: int = 0) -> list[dict[str, str]]:

    errors: list[dict[str, str]] = []

    if not isinstance(contract, dict) or depth > 32:

        return [{"path": path, "code": "invalid_contract"}]

    kind = contract.get("type")

    supported = {"object", "array", "string", "integer", "number", "boolean", "null", "text", "any"}

    if kind not in supported:

        return [{"path": path, "code": "unsupported_type"}]

    if kind in {"text", "string"}:

        if not isinstance(value, str):

            errors.append({"path": path, "code": "type_mismatch"})

        elif "minLength" in contract and (not isinstance(contract["minLength"], int) or contract["minLength"] < 0 or len(value) < contract["minLength"]):

            errors.append({"path": path, "code": "min_length"})

        elif "maxLength" in contract and (not isinstance(contract["maxLength"], int) or contract["maxLength"] < 0 or len(value) > contract["maxLength"]):

            errors.append({"path": path, "code": "max_length"})

        if "enum" in contract and value not in contract["enum"]:

            errors.append({"path": path, "code": "enum_mismatch"})

        return errors

    if kind == "object":

        if not isinstance(value, dict):

            return [{"path": path, "code": "type_mismatch"}]

        required = contract.get("required", [])

        props = contract.get("properties", {})

        if not isinstance(required, list) or any(not isinstance(key, str) for key in required) or not isinstance(props, dict) or any(not isinstance(key, str) or not isinstance(spec, dict) for key, spec in props.items()):

            return [{"path": path, "code": "invalid_contract"}]

        for key in required:

            if key not in value:

                errors.append({"path": f"{path}.{key}", "code": "required"})

        if contract.get("additionalProperties", True) is False:

            for key in value.keys() - props.keys():

                errors.append({"path": f"{path}.{key}", "code": "additional_property"})

        for key, spec in props.items():

            if key in value:

                errors.extend(_contract_errors(value[key], spec, f"{path}.{key}", depth + 1))

        return errors

    if kind == "array":

        if not isinstance(value, list):

            return [{"path": path, "code": "type_mismatch"}]

        for field, predicate, code in (("minItems", lambda n: len(value) >= n, "min_items"), ("maxItems", lambda n: len(value) <= n, "max_items")):

            if field in contract and (not isinstance(contract[field], int) or contract[field] < 0 or not predicate(contract[field])):

                errors.append({"path": path, "code": code})

        items = contract.get("items")

        if not isinstance(items, dict):

            return errors + [{"path": path, "code": "invalid_contract"}]

        for index, item in enumerate(value):

            errors.extend(_contract_errors(item, items, f"{path}[{index}]", depth + 1))

        return errors

    if kind in {"integer", "number"}:

        valid_type = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and (kind != "integer" or isinstance(value, int))

        if not valid_type:

            return [{"path": path, "code": "type_mismatch"}]

        for field, predicate, code in (("minimum", lambda n: value >= n, "minimum"), ("maximum", lambda n: value <= n, "maximum")):

            if field in contract:

                bound = contract[field]

                if isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound):

                    errors.append({"path": path, "code": "invalid_contract"})

                elif not predicate(bound):

                    errors.append({"path": path, "code": code})

        return errors

    if kind == "boolean" and not isinstance(value, bool):

        errors.append({"path": path, "code": "type_mismatch"})

    elif kind == "null" and value is not None:

        errors.append({"path": path, "code": "type_mismatch"})

    return errors





def _contract_valid(value: Any, contract: dict[str, Any]) -> bool:

    return not _contract_errors(value, contract)





def _business_assertion_errors(value: Any, assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    errors = []
    for assertion in assertions:
        current = value
        path = assertion["path"]
        parts = path[2:].split(".") if path.startswith("$.") else []
        if not parts and path != "$":
            errors.append({"path": path, "code": "invalid_path"})
            continue
        missing = False
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                missing = True
                break
        if missing or current != assertion["expected"]:
            errors.append({"path": path, "code": "expected_value_mismatch", "expected": assertion["expected"], "actual": None if missing else current})
    return errors

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

        self.recovery_events: list[dict[str, Any]] = []
        self._active_executions: dict[str, dict[str, Any]] = {}
        self._active_executions_lock = threading.RLock()
        self._create_schema()
        self._recover_orphan_version_dirs()



    def _recover_orphan_version_dirs(self) -> None:
        """Quarantine version directories written before their DB row."""
        if not self.tools_root.exists():
            return
        for member_dir in self.tools_root.iterdir():
            tools_dir = member_dir / "tools"
            if not member_dir.is_dir() or not tools_dir.is_dir():
                continue
            for tool_dir in tools_dir.iterdir():
                if not tool_dir.is_dir() or tool_dir.name == ".orphaned":
                    continue
                for staging_dir in tool_dir.iterdir():
                    if staging_dir.is_dir() and staging_dir.name.startswith(".v") and staging_dir.name.endswith(".staging"):
                        self._quarantine_version_path(tool_dir.name, 0, staging_dir)
                known = {row[0] for row in self.store.db.execute("SELECT version FROM tool_versions WHERE tool_id=?", (tool_dir.name,))}
                for version_dir in tool_dir.iterdir():
                    if not version_dir.is_dir() or not version_dir.name.startswith("v") or not version_dir.name[1:].isdigit():
                        continue
                    version = int(version_dir.name[1:])
                    if version in known:
                        continue
                    quarantine = tool_dir / ".orphaned"
                    quarantine.mkdir(exist_ok=True)
                    target = quarantine / version_dir.name
                    if target.exists():
                        target = quarantine / f"{version_dir.name}-{uuid.uuid4().hex}"
                    shutil.move(str(version_dir), str(target))
                    self.recovery_events.append({"tool_id": tool_dir.name, "version": version, "path": str(target), "action": "quarantined_orphan"})

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

            independent_key TEXT NOT NULL, execution_id TEXT, input_hash TEXT, expected_output TEXT,
            created_at REAL NOT NULL, UNIQUE(tool_id, version, independent_key)

        );

        CREATE TABLE IF NOT EXISTS tool_shares (

            share_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL, version INTEGER NOT NULL,

            owner_member_id TEXT NOT NULL, recipient_member_id TEXT NOT NULL, permission TEXT NOT NULL,

            status TEXT NOT NULL, created_at REAL NOT NULL, issue_id TEXT, max_uses INTEGER, used_count INTEGER NOT NULL DEFAULT 0, expires_at REAL,
            UNIQUE(tool_id, version, recipient_member_id)

        );

        CREATE TABLE IF NOT EXISTS tool_issues (

            issue_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL, version INTEGER NOT NULL,

            reporter_member_id TEXT NOT NULL, execution_id TEXT, description TEXT NOT NULL,

            severity TEXT NOT NULL, status TEXT NOT NULL, repair_version INTEGER,

            diagnosis TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            review_member_id TEXT, review_execution_id TEXT, review_evidence TEXT,
            triage_owner_member_id TEXT, triage_reason TEXT, duplicate_of TEXT,
            requested_information TEXT, rejection_reason TEXT, escalated_at REAL,
            due_at REAL, environment TEXT NOT NULL DEFAULT '{}', input_summary TEXT,
            expected_output TEXT, actual_output TEXT, reproduction_steps TEXT, impact_scope TEXT, escalation_to_member_id TEXT

        );

        CREATE TABLE IF NOT EXISTS tool_issue_events (
            event_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, event_type TEXT NOT NULL,
            actor_member_id TEXT NOT NULL, from_status TEXT, to_status TEXT, payload TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE, created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tool_issue_pairs (
            pair_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, repair_version INTEGER NOT NULL,
            environment TEXT NOT NULL, input_hash TEXT NOT NULL, before_outcome TEXT NOT NULL,
            after_outcome TEXT NOT NULL, created_at REAL NOT NULL,
            before_execution_id TEXT, after_execution_id TEXT,
            UNIQUE(issue_id, repair_version, environment, input_hash)
        );

        CREATE TABLE IF NOT EXISTS tool_issue_escalations (
            work_item_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, dispatcher_member_id TEXT NOT NULL,
            status TEXT NOT NULL, reason TEXT NOT NULL, claimed_by TEXT, resolution TEXT,
            created_at REAL NOT NULL, claimed_at REAL, completed_at REAL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tool_issue_escalation_events (
            event_id TEXT PRIMARY KEY, work_item_id TEXT NOT NULL, event_type TEXT NOT NULL,
            actor_member_id TEXT NOT NULL, payload TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS host_authorizations (
            authorization_id TEXT PRIMARY KEY, owner_member_id TEXT NOT NULL, tool_id TEXT NOT NULL,
            version INTEGER NOT NULL, capabilities TEXT NOT NULL, status TEXT NOT NULL,
            reason TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL, revoke_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_host_auth_tool ON host_authorizations(tool_id, version, owner_member_id, status);

        CREATE TABLE IF NOT EXISTS tool_circuit_breakers (
            tool_id TEXT NOT NULL, version INTEGER NOT NULL, scope TEXT NOT NULL,
            reason TEXT NOT NULL, status TEXT NOT NULL, opened_at REAL NOT NULL,
            recovered_at REAL, recovered_by TEXT, PRIMARY KEY(tool_id, version, scope)
        );

        CREATE INDEX IF NOT EXISTS idx_tools_owner_status ON tool_assets(owner_member_id, status);

        CREATE INDEX IF NOT EXISTS idx_tool_verify ON tool_verifications(tool_id, version, outcome);

        CREATE TABLE IF NOT EXISTS tool_executions (

            execution_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL, version INTEGER NOT NULL,

            actor_member_id TEXT NOT NULL, task_id TEXT, input_hash TEXT NOT NULL,

            dry_run INTEGER NOT NULL, status TEXT NOT NULL, exit_code INTEGER,

            stdout TEXT NOT NULL, stderr TEXT NOT NULL, termination_reason TEXT,

            permissions TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
            runtime_backend TEXT NOT NULL DEFAULT 'python_host', isolation_level TEXT NOT NULL DEFAULT 'host_process',
            isolation_capabilities TEXT NOT NULL DEFAULT '{}', business_assertions TEXT NOT NULL DEFAULT '[]'

        );

        """)
        # Older databases had a UNIQUE constraint over status, which rejects a
        # legitimate rejection chain after the same reason is reused. Rebuild
        # only that legacy table and preserve every row and audit event.
        escalation_indexes = self.store.db.execute("PRAGMA index_list(tool_issue_escalations)").fetchall()
        legacy_unique = any(index[2] and [item[2] for item in self.store.db.execute(f"PRAGMA index_info({index[1]})").fetchall()] == ["issue_id", "dispatcher_member_id", "reason", "status"] for index in escalation_indexes)
        if legacy_unique:
            self.store.db.execute("ALTER TABLE tool_issue_escalations RENAME TO tool_issue_escalations_legacy")
            self.store.db.execute("""CREATE TABLE tool_issue_escalations (
                work_item_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, dispatcher_member_id TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL, claimed_by TEXT, resolution TEXT,
                created_at REAL NOT NULL, claimed_at REAL, completed_at REAL, updated_at REAL NOT NULL
            )""")
            self.store.db.execute("""INSERT INTO tool_issue_escalations
                (work_item_id, issue_id, dispatcher_member_id, status, reason, claimed_by, resolution,
                 created_at, claimed_at, completed_at, updated_at)
                SELECT work_item_id, issue_id, dispatcher_member_id, status, reason, claimed_by, resolution,
                       created_at, claimed_at, completed_at, updated_at
                FROM tool_issue_escalations_legacy""")
            self.store.db.execute("DROP TABLE tool_issue_escalations_legacy")
        self.store.db.execute("CREATE INDEX IF NOT EXISTS idx_tool_issue_escalations_dispatcher_status ON tool_issue_escalations(dispatcher_member_id, status)")
        share_columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(tool_shares)")}
        for name, definition in (("issue_id", "TEXT"), ("max_uses", "INTEGER"), ("used_count", "INTEGER NOT NULL DEFAULT 0"), ("expires_at", "REAL")):
            if name not in share_columns:
                self.store.db.execute(f"ALTER TABLE tool_shares ADD COLUMN {name} {definition}")
        issue_columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(tool_issues)")}
        for name, definition in (("review_member_id", "TEXT"), ("review_execution_id", "TEXT"), ("review_evidence", "TEXT"),
                                  ("triage_owner_member_id", "TEXT"), ("triage_reason", "TEXT"), ("duplicate_of", "TEXT"),
                                  ("requested_information", "TEXT"), ("rejection_reason", "TEXT"), ("escalated_at", "REAL"),
                                  ("due_at", "REAL"), ("environment", "TEXT NOT NULL DEFAULT '{}'"), ("input_summary", "TEXT"),
                                  ("expected_output", "TEXT"), ("actual_output", "TEXT"), ("reproduction_steps", "TEXT"), ("impact_scope", "TEXT"), ("escalation_to_member_id", "TEXT")):
            if name not in issue_columns:
                self.store.db.execute(f"ALTER TABLE tool_issues ADD COLUMN {name} {definition}")
        verification_columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(tool_verifications)")}
        for name, definition in (("execution_id", "TEXT"), ("input_hash", "TEXT"), ("expected_output", "TEXT")):
            if name not in verification_columns:
                self.store.db.execute(f"ALTER TABLE tool_verifications ADD COLUMN {name} {definition}")
        pair_columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(tool_issue_pairs)")}
        for name, definition in (("before_execution_id", "TEXT"), ("after_execution_id", "TEXT")):
            if name not in pair_columns:
                self.store.db.execute(f"ALTER TABLE tool_issue_pairs ADD COLUMN {name} {definition}")
        columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(tool_executions)")}
        for name, definition in (("runtime_backend", "TEXT NOT NULL DEFAULT 'python_host'"), ("isolation_level", "TEXT NOT NULL DEFAULT 'host_process'"), ("isolation_capabilities", "TEXT NOT NULL DEFAULT '{}'"), ("business_assertions", "TEXT NOT NULL DEFAULT '[]'")):
            if name not in columns:
                self.store.db.execute(f"ALTER TABLE tool_executions ADD COLUMN {name} {definition}")



    def execute(self, actor_member_id: str, tool_id: str, version: int, input_data: Any = None, *, dry_run: bool = False, verification_only: bool = False, task_id: str | None = None, timeout_seconds: float = 10.0, max_output_bytes: int = 65536, share_id: str | None = None, security_review_issue_id: str | None = None) -> dict[str, Any]:

        tool = self._tool(tool_id)

        breaker = self.store.db.execute("SELECT 1 FROM tool_circuit_breakers WHERE tool_id=? AND version=? AND status='open'", (tool_id, version)).fetchone()
        if breaker:
            raise ValueError("tool_circuit_breaker_open")

        access = actor_member_id == tool["owner_member_id"]
        share = None
        security_review = None
        if not access and security_review_issue_id:
            security_review = self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=? AND severity='security' AND status='security_review' AND tool_id=? AND repair_version=?", (security_review_issue_id, tool_id, version)).fetchone()
            access = bool(security_review and actor_member_id not in {security_review["reporter_member_id"], tool["owner_member_id"]})

        if not access and share_id:

            share = self.store.db.execute("SELECT * FROM tool_shares WHERE share_id=? AND tool_id=? AND recipient_member_id=?", (share_id, tool_id, actor_member_id)).fetchone()

            access = bool(share and share["status"] in {"trial", "active"} and share["permission"] == "team_execute" and share["version"] == version and (share["expires_at"] is None or share["expires_at"] > time.time()) and (share["max_uses"] is None or share["used_count"] < share["max_uses"]))

        if not access:

            raise ValueError("tool_execute_denied")

        verify_mode = verification_only and actor_member_id == tool["owner_member_id"] and tool["status"] in {"draft", "candidate"}
        security_review_mode = bool(security_review and verification_only)

        repair_trial = bool(actor_member_id != tool["owner_member_id"] and share and share["issue_id"] and share["status"] == "trial") if not access or actor_member_id != tool["owner_member_id"] else False
        if security_review and tool["status"] not in {"draft", "candidate", "active"}:
            raise ValueError("security_review_version_unavailable")
        if tool["status"] != "active" and not verify_mode and not repair_trial and not security_review_mode:

            raise ValueError("only_active_tool_can_execute")

        if verification_only and not verify_mode and not security_review:

            raise ValueError("verification_execution_owner_candidate_only")

        if version != tool["current_version"] and actor_member_id != tool["owner_member_id"] and not security_review:
            repair_share = self.store.db.execute("SELECT issue_id, status, permission, expires_at, max_uses, used_count FROM tool_shares WHERE share_id=? AND tool_id=? AND recipient_member_id=? AND version=?", (share_id, tool_id, actor_member_id, version)).fetchone()
            if not repair_share or not repair_share["issue_id"] or repair_share["status"] != "trial":
                raise ValueError("repair_version_requires_owner_trial")

        row = self.store.db.execute("SELECT * FROM tool_versions WHERE tool_id=? AND version=?", (tool_id, version)).fetchone()

        if not row:

            raise ValueError("tool_version_not_found")

        manifest = json.loads(row["manifest"])

        source = Path(row["source_path"]).resolve()

        version_root = (self._member_dir(tool["owner_member_id"]) / tool_id / f"v{version}").resolve()

        if not source.is_relative_to(version_root) or not source.is_file():

            raise ValueError("tool_source_path_invalid")

        entrypoint = manifest.get("entrypoint") or ["python", "tool.py"]
        expected_source = {"python": "tool.py", "node": "tool.js", "powershell": "tool.ps1"}
        if not isinstance(entrypoint, list) or len(entrypoint) != 2 or entrypoint[0] not in expected_source or entrypoint[1] != expected_source[entrypoint[0]] or source.name != expected_source[entrypoint[0]]:

            raise ValueError("tool_entrypoint_not_allowlisted")

        raw = source.read_bytes()

        actual_hash = hashlib.sha256(raw).hexdigest()

        manifest_digest = manifest.get("manifest_hash")

        unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}

        expected_manifest_digest = hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

        if actual_hash != row["content_hash"] or manifest.get("content_hash") != actual_hash or manifest_digest != expected_manifest_digest:

            raise ValueError("tool_content_hash_mismatch")

        permissions = manifest.get("permissions")

        if not isinstance(permissions, list) or "execute" not in permissions:

            raise ValueError("tool_execute_permission_missing")

        if len(set(permissions)) != len(permissions):
            raise ValueError("tool_permissions_duplicate")
        unsupported = set(permissions) - {"execute", "cpu"}

        if unsupported:

            raise ValueError("tool_permission_not_supported")

        isolation = manifest.get("isolation", {})
        isolation_capability_names = {"filesystem_isolation", "network_isolation", "subprocess_isolation", "memory_limit", "file_size_limit", "process_tree_control"}
        if not isinstance(isolation, dict):
            raise ValueError("tool_isolation_requirements_invalid")
        required_isolation = isolation.get("requires", [])
        if not isinstance(required_isolation, list) or any(not isinstance(item, str) or item not in isolation_capability_names for item in required_isolation):
            raise ValueError("tool_isolation_requirements_invalid")
        if required_isolation:
            raise ValueError("tool_isolation_backend_unavailable:python_host:" + ",".join(sorted(set(required_isolation))))
        runtime_profile = isolation.get("profile", "low_risk_host")
        if runtime_profile == "host_trusted":
            authorization = self.store.db.execute("SELECT authorization_id FROM host_authorizations WHERE owner_member_id=? AND tool_id=? AND version=? AND status='active' ORDER BY created_at DESC LIMIT 1", (actor_member_id, tool_id, version)).fetchone()
            if not authorization:
                revoked = self.store.db.execute("SELECT 1 FROM host_authorizations WHERE owner_member_id=? AND tool_id=? AND version=? AND status='revoked' LIMIT 1", (actor_member_id, tool_id, version)).fetchone()
                raise ValueError("host_authorization_revoked" if revoked else "host_authorization_required")
        if runtime_profile not in {"low_risk_host", "host_trusted"}:
            raise ValueError("tool_isolation_profile_unsupported")
        assertions = manifest.get("business_assertions", [])
        if not isinstance(assertions, list) or any(not isinstance(item, dict) or not isinstance(item.get("path"), str) or "expected" not in item for item in assertions):
            raise ValueError("tool_business_assertions_invalid")
        interpreter_names = {"python": sys.executable, "node": "node", "powershell": "powershell"}
        interpreter = interpreter_names[entrypoint[0]]
        resolved_interpreter = shutil.which(interpreter)
        if not resolved_interpreter:
            raise ValueError("tool_interpreter_unavailable:" + entrypoint[0])

        timeout = _finite(timeout_seconds, "timeout_seconds", nonnegative=True)

        output_limit = int(_finite(max_output_bytes, "max_output_bytes", nonnegative=True))

        if timeout <= 0 or timeout > 300 or output_limit <= 0 or output_limit > 4 * 1024 * 1024:

            raise ValueError("tool_resource_limit_invalid")

        input_errors = _contract_errors(input_data, manifest.get("input_contract", {}))

        if input_errors:

            raise ValueError("tool_input_contract_invalid:" + json.dumps(input_errors, separators=(",", ":")))

        encoded = json.dumps(input_data, ensure_ascii=False, sort_keys=True).encode("utf-8")

        if len(encoded) > 1024 * 1024:

            raise ValueError("tool_input_size_limit")

        input_hash = hashlib.sha256(encoded).hexdigest()

        execution_id = f"exec_{uuid.uuid4().hex}"

        started = time.time()

        runtime_backend = "host_trusted" if runtime_profile == "host_trusted" else "python_host"
        isolation_level = "same_account_host" if runtime_profile == "host_trusted" else "host_process"
        runtime_capabilities = {"filesystem_isolation": False, "network_isolation": False, "subprocess_isolation": False, "memory_limit": False, "file_size_limit": False, "process_tree_control": False, "working_directory": "temporary_only_not_a_security_boundary", "timeout": True, "output_limit": True, "active_cancellation": False, "host_trusted": runtime_profile == "host_trusted", "same_account_no_os_isolation": runtime_profile == "host_trusted", "interpreter": resolved_interpreter, "interpreter_kind": entrypoint[0], "content_hash": actual_hash, "manifest_hash": manifest_digest, "source_path": str(source)}
        business_assertions = manifest.get("business_assertions", [])
        common = {"execution_id": execution_id, "tool_id": tool_id, "version": version, "actor_member_id": actor_member_id, "task_id": task_id, "input_hash": input_hash, "permissions": permissions, "runtime_backend": runtime_backend, "isolation_level": isolation_level, "isolation_capabilities": runtime_capabilities, "business_assertions": business_assertions, "verification_only": bool(verification_only)}

        if dry_run:

            self.store.db.execute("INSERT INTO tool_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (execution_id, tool_id, version, actor_member_id, task_id, input_hash, 1, "dry_run", None, "", "", "dry_run", json.dumps(permissions), started, started, runtime_backend, isolation_level, json.dumps(runtime_capabilities), json.dumps(business_assertions)))

            self._event(tool_id, version, "execution_dry_run", {"execution_id": execution_id, "actor_member_id": actor_member_id, "permissions": permissions}, f"tool:execution:{execution_id}")

            common["argv"] = [resolved_interpreter, "-I", str(source)] if entrypoint[0] == "python" else [resolved_interpreter, str(source)]
            return {**common, "status": "dry_run", "argv": common["argv"], "permissions": permissions}

        current = self.store.db.execute("SELECT status, current_version, owner_member_id FROM tool_assets WHERE tool_id=?", (tool_id,)).fetchone()
        repair_version_access = bool(repair_trial and share and share["version"] == version and share["issue_id"])
        if not current or (current["status"] != "active" and not (verification_only and actor_member_id == current["owner_member_id"] and current["status"] in {"draft", "candidate"}) and not repair_version_access and not security_review_mode) or (version != current["current_version"] and actor_member_id != current["owner_member_id"] and not repair_version_access and not security_review_mode):
            raise ValueError("tool_version_state_changed")
        current_share = None
        if actor_member_id != current["owner_member_id"] and not security_review_mode:
            current_share = self.store.db.execute("SELECT status, permission, version, issue_id, max_uses, used_count, expires_at FROM tool_shares WHERE share_id=? AND tool_id=? AND recipient_member_id=?", (share_id, tool_id, actor_member_id)).fetchone()
            if not current_share or current_share["status"] not in {"trial", "active"} or current_share["permission"] != "team_execute" or current_share["version"] != version:
                raise ValueError("tool_execute_denied")
            if current_share["expires_at"] is not None and current_share["expires_at"] <= time.time():
                raise ValueError("tool_trial_expired")
            if current_share["max_uses"] is not None and current_share["used_count"] >= current_share["max_uses"]:
                raise ValueError("tool_trial_exhausted")
        result_status, exit_code, stdout, stderr, reason = "failed", None, "", "", None

        decoded = None

        try:

            with tempfile.TemporaryDirectory(prefix="butian-tool-") as work:

                argv = [resolved_interpreter, "-I", str(source)] if entrypoint[0] == "python" else [resolved_interpreter, str(source)]
                runtime_capabilities["argv"] = argv
                runtime_capabilities["cwd"] = work
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=work, env={"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}, shell=False)

                with self._active_executions_lock:
                    self._active_executions[execution_id] = {"process": proc, "actor_member_id": actor_member_id, "tool_id": tool_id, "version": version, "started_at": started, "cancel_requested": False}
                self.store.db.execute("INSERT INTO tool_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (execution_id, tool_id, version, actor_member_id, task_id, input_hash, 0, "running", None, "", "", None, json.dumps(permissions), started, None, runtime_backend, isolation_level, json.dumps(runtime_capabilities), json.dumps(business_assertions)))
                self._event(tool_id, version, "execution_started", {"execution_id": execution_id, "actor_member_id": actor_member_id}, f"tool:execution:{execution_id}:started")

                captured: dict[str, bytes] = {"stdout": b"", "stderr": b""}
                overflow = threading.Event()

                def drain(name, pipe):
                    chunks = bytearray()
                    while True:
                        chunk = pipe.read(8192)
                        if not chunk:
                            break
                        remaining = output_limit - len(chunks)
                        if remaining > 0:
                            chunks.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            overflow.set()
                            try:
                                _terminate_process_tree(proc)
                            except OSError:
                                pass
                            break
                    captured[name] = bytes(chunks)

                if current_share is not None and current_share["issue_id"]:
                    updated = self.store.db.execute("UPDATE tool_shares SET used_count=used_count+1 WHERE share_id=? AND status='trial' AND used_count < COALESCE(max_uses, 2147483647) AND (expires_at IS NULL OR expires_at>?)", (share_id, time.time()))
                    if updated.rowcount != 1:
                        _terminate_process_tree(proc)
                        proc.wait()
                        raise ValueError("tool_trial_exhausted")
                readers = [threading.Thread(target=drain, args=("stdout", proc.stdout), daemon=True), threading.Thread(target=drain, args=("stderr", proc.stderr), daemon=True)]
                for reader in readers:
                    reader.start()
                try:
                    proc.stdin.write(encoded)
                    proc.stdin.close()
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    _terminate_process_tree(proc)
                    proc.wait()
                    result_status, reason = "failed", "timeout"
                except (BrokenPipeError, OSError):
                    proc.wait()
                finally:
                    for reader in readers:
                        reader.join(timeout=2)
                exit_code = proc.returncode
                with self._active_executions_lock:
                    active = self._active_executions.get(execution_id)
                    cancel_requested = bool(active and active["cancel_requested"])
                    self._active_executions.pop(execution_id, None)
                if cancel_requested:
                    result_status = "cancelled" if proc.poll() is not None else "unknown"
                    reason = "cancelled_by_actor" if result_status == "cancelled" else "cancel_unconfirmed"
                out, err = captured["stdout"], captured["stderr"]
                stdout, stderr = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
                if not cancel_requested and reason != "timeout":
                    result_status = "failed" if overflow.is_set() or exit_code else "success"
                    reason = "output_limit_exceeded" if overflow.is_set() else (None if exit_code == 0 else "nonzero_exit")
                    if result_status == "success":

                        contract = manifest.get("output_contract", {})

                        try:

                            decoded = stdout if contract.get("type") in {"text", "string"} else json.loads(stdout)

                        except (TypeError, ValueError):

                            result_status, reason = "failed", "output_contract_invalid_json"

                        else:

                            output_errors = _contract_errors(decoded, contract)

                            assertion_errors = _business_assertion_errors(decoded, business_assertions) if not output_errors else []
                            if assertion_errors:
                                result_status, reason = "failed", "business_assertion_failed:" + json.dumps(assertion_errors, separators=(",", ":"))
                            if output_errors:

                                result_status, reason = "failed", "output_contract_invalid:" + json.dumps(output_errors, separators=(",", ":"))

        except OSError as exc:

            stderr, reason = str(exc)[:output_limit], "launch_failed"
            if not self.store.db.execute("SELECT 1 FROM tool_executions WHERE execution_id=?", (execution_id,)).fetchone():
                self.store.db.execute("INSERT INTO tool_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (execution_id, tool_id, version, actor_member_id, task_id, input_hash, 0, "failed", None, "", stderr, reason, json.dumps(permissions), started, time.time(), runtime_backend, isolation_level, json.dumps(runtime_capabilities), json.dumps(business_assertions)))

        finished = time.time()

        self.store.db.execute("UPDATE tool_executions SET status=?, exit_code=?, stdout=?, stderr=?, termination_reason=?, finished_at=? WHERE execution_id=?", (result_status, exit_code, stdout, stderr, reason, finished, execution_id))
        self._event(tool_id, version, "execution", {"execution_id": execution_id, "actor_member_id": actor_member_id, "status": result_status, "reason": reason}, f"tool:execution:{execution_id}")

        if result_status in {"failed", "cancelled"}:

            self.report_issue(actor_member_id, tool_id, version, description=f"Execution {execution_id}: {reason}", execution_id=execution_id)

        return {**common, "status": result_status, "exit_code": exit_code, "stdout": stdout, "stderr": stderr, "termination_reason": reason, "input_hash": input_hash, "expected_output": decoded if result_status == "success" else None}



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



    def report_issue(self, actor, tool_id, version, *, description, execution_id=None, severity="bug", environment=None, input_summary=None, expected_output=None, actual_output=None, reproduction_steps=None, impact_scope=None):

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

        initial_status = "security_review" if severity == "security" else "open"
        owner = self._tool(tool_id)["owner_member_id"]
        deadline = now + 86400
        self.store.db.execute("INSERT INTO tool_issues (issue_id, tool_id, version, reporter_member_id, execution_id, description, severity, status, repair_version, diagnosis, created_at, updated_at, review_member_id, review_execution_id, review_evidence, triage_owner_member_id, due_at, environment, input_summary, expected_output, actual_output, reproduction_steps, impact_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (issue_id, tool_id, version, actor, execution_id, description, severity, initial_status, None, None, now, now, None, None, None, owner, deadline, json.dumps(environment or {}, ensure_ascii=False, sort_keys=True), input_summary, json.dumps(expected_output, ensure_ascii=False) if expected_output is not None else None, json.dumps(actual_output, ensure_ascii=False) if actual_output is not None else None, reproduction_steps, impact_scope))

        self._event(tool_id, version, "issue_reported", {"issue_id": issue_id, "reporter": actor, "severity": severity}, "issue:" + issue_id)
        self._issue_event(issue_id, actor, "reported", None, initial_status, {"severity": severity}, "issue:" + issue_id + ":reported")

        return dict(self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?", (issue_id,)).fetchone())



    def list_issues(self, actor, tool_id):

        tool = self._tool(tool_id)

        self._member_dir(actor)

        if actor != tool["owner_member_id"] and not self.store.db.execute("SELECT 1 FROM tool_shares WHERE tool_id=? AND recipient_member_id=?", (tool_id, actor)).fetchone():

            raise ValueError("tool_access_denied")

        if actor != tool["owner_member_id"]:
            valid_share = self.store.db.execute("SELECT 1 FROM tool_shares WHERE tool_id=? AND recipient_member_id=? AND status IN ('active','trial') AND (expires_at IS NULL OR expires_at>?) AND permission IN ('team_readonly','team_execute')", (tool_id, actor, time.time())).fetchone()
            if not valid_share:
                raise ValueError("tool_access_denied")
        return [dict(row) for row in self.store.db.execute("SELECT * FROM tool_issues WHERE tool_id=? ORDER BY created_at", (tool_id,))]



    def repair_issue(self, owner_member_id, issue_id, *, repair_version, diagnosis):

        row = self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?", (issue_id,)).fetchone()

        if not row:

            raise ValueError("tool_issue_not_found")

        tool = self._tool(row["tool_id"])

        if tool["owner_member_id"] != owner_member_id:

            raise ValueError("tool_access_denied")

        if not isinstance(diagnosis, str) or not diagnosis.strip():
            raise ValueError("tool_issue_diagnosis_required")
        if not isinstance(repair_version, int) or repair_version <= int(row["version"]):
            raise ValueError("tool_repair_version_must_be_newer")
        if not self.store.db.execute("SELECT 1 FROM tool_versions WHERE tool_id=? AND version=?", (row["tool_id"], repair_version)).fetchone():

            raise ValueError("tool_version_not_found")

        allowed_statuses = {"open", "reopened", "triaged", "repairing"} if row["severity"] != "security" else {"security_review", "reopened", "triaged", "repairing"}
        if row["status"] not in allowed_statuses:
            raise ValueError("tool_issue_not_repairable")
        if row["severity"] == "security":
            self._member_dir(owner_member_id)
            now = time.time()
            self.store.db.execute("BEGIN")
            try:
                self.store.db.execute("UPDATE tool_issues SET status='security_review', repair_version=?, diagnosis=?, updated_at=? WHERE issue_id=?", (repair_version, diagnosis, now, issue_id))
                self._event(row["tool_id"], repair_version, "security_repair_proposed", {"issue_id": issue_id, "repair_version": repair_version}, f"issue:{issue_id}:security-repair:{repair_version}")
                self.store.db.commit()
            except BaseException:
                self.store.db.rollback()
                raise
            return dict(self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?", (issue_id,)).fetchone())
        now = time.time()
        trial_share_id = f"share_{uuid.uuid4().hex}"
        expires_at = now + 7 * 24 * 60 * 60
        self.store.db.execute("BEGIN")
        try:
            existing = self.store.db.execute("SELECT share_id, status FROM tool_shares WHERE tool_id=? AND version=? AND recipient_member_id=?", (row["tool_id"], repair_version, row["reporter_member_id"])).fetchone()
            if existing and existing["status"] != "revoked":
                raise ValueError("tool_repair_trial_share_already_exists")
            if existing:
                self.store.db.execute("DELETE FROM tool_shares WHERE share_id=? AND status='revoked'", (existing["share_id"],))
            self.store.db.execute("UPDATE tool_issues SET status='awaiting_confirmation', repair_version=?, diagnosis=?, updated_at=? WHERE issue_id=?", (repair_version, diagnosis, now, issue_id))
            self.store.db.execute(
                "INSERT INTO tool_shares (share_id, tool_id, version, owner_member_id, recipient_member_id, permission, status, created_at, issue_id, max_uses, used_count, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (trial_share_id, row["tool_id"], repair_version, owner_member_id, row["reporter_member_id"], "team_execute", "trial", now, issue_id, 3, 0, expires_at),
            )
            self._event(row["tool_id"], repair_version, "issue_repair_proposed", {"issue_id": issue_id, "repair_version": repair_version, "trial_share_id": trial_share_id, "trial_expires_at": expires_at, "trial_max_uses": 3}, f"issue:{issue_id}:repair:{repair_version}")
            self.store.db.commit()
        except BaseException:
            self.store.db.rollback()
            raise
        return {**dict(self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?", (issue_id,)).fetchone()), "trial_share_id": trial_share_id, "trial_expires_at": expires_at, "trial_max_uses": 3}



    def confirm_repair(self, confirmer_member_id, issue_id, *, execution_id, resolved):

        row = self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?", (issue_id,)).fetchone()

        if not row:

            raise ValueError("tool_issue_not_found")

        if confirmer_member_id != row["reporter_member_id"]:

            raise ValueError("tool_repair_confirmation_requires_reporter")

        if row["status"] != "awaiting_confirmation" or not row["repair_version"]:

            raise ValueError("tool_issue_not_awaiting_confirmation")

        execution = self.store.db.execute("SELECT * FROM tool_executions WHERE execution_id=?", (execution_id,)).fetchone()

        if not execution or (execution["tool_id"], execution["version"], execution["actor_member_id"]) != (row["tool_id"], row["repair_version"], confirmer_member_id):
            raise ValueError("tool_repair_execution_mismatch")
        if bool(resolved) and execution["status"] != "success":
            raise ValueError("tool_repair_execution_mismatch")
        if not bool(resolved) and execution["status"] not in {"success", "failed", "cancelled"}:
            raise ValueError("tool_repair_execution_mismatch")

        status = "resolved" if resolved else "reopened"
        now = time.time()
        self.store.db.execute("BEGIN")
        try:
            updated = self.store.db.execute("UPDATE tool_issues SET status=?, updated_at=? WHERE issue_id=? AND status='awaiting_confirmation'", (status, now, issue_id))
            if updated.rowcount != 1:
                raise ValueError("tool_issue_not_awaiting_confirmation")
            self.store.db.execute("UPDATE tool_shares SET status='revoked' WHERE issue_id=? AND status='trial'", (issue_id,))
            self._event(row["tool_id"], row["repair_version"], "issue_repair_confirmed", {"issue_id": issue_id, "status": status, "execution_id": execution_id}, f"issue:{issue_id}:confirm:{execution_id}")
            self.store.db.commit()
        except BaseException:
            self.store.db.rollback()
            raise

        return dict(self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?", (issue_id,)).fetchone())



    def review_security_issue(self, reviewer_member_id, issue_id, *, execution_id, evidence_ids, resolved):
        row = self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?", (issue_id,)).fetchone()
        if not row:
            raise ValueError("tool_issue_not_found")
        if row["severity"] != "security" or row["status"] not in {"security_review", "reopened"} or not row["repair_version"]:
            raise ValueError("tool_security_review_not_pending")
        tool = self._tool(row["tool_id"])
        self._member_dir(reviewer_member_id)
        if reviewer_member_id in {row["reporter_member_id"], tool["owner_member_id"]}:
            raise ValueError("tool_security_review_requires_independent_reviewer")
        if not isinstance(evidence_ids, list) or not evidence_ids or any(not isinstance(item, str) or not item for item in evidence_ids):
            raise ValueError("tool_security_review_evidence_required")
        execution = self.store.db.execute("SELECT * FROM tool_executions WHERE execution_id=?", (execution_id,)).fetchone()
        if not execution or (execution["tool_id"], execution["version"], execution["actor_member_id"], execution["status"]) != (row["tool_id"], row["repair_version"], reviewer_member_id, "success"):
            raise ValueError("tool_security_review_execution_mismatch")
        for evidence_id in dict.fromkeys(evidence_ids):
            verified = self.store.verify_evidence(evidence_id, actor_member_id=reviewer_member_id)
            if verified["status"] != "valid":
                raise ValueError("tool_security_review_evidence_invalid")
        status = "resolved" if resolved else "reopened"
        now = time.time()
        self.store.db.execute("BEGIN")
        try:
            updated = self.store.db.execute("UPDATE tool_issues SET status=?, review_member_id=?, review_execution_id=?, review_evidence=?, updated_at=? WHERE issue_id=? AND status IN ('security_review','reopened')", (status, reviewer_member_id, execution_id, json.dumps(list(dict.fromkeys(evidence_ids))), now, issue_id))
            if updated.rowcount != 1:
                raise ValueError("tool_security_review_not_pending")
            self._event(row["tool_id"], row["repair_version"], "security_issue_reviewed", {"issue_id": issue_id, "status": status, "reviewer": reviewer_member_id, "execution_id": execution_id, "evidence_ids": list(dict.fromkeys(evidence_ids))}, f"issue:{issue_id}:security-review:{execution_id}")
            self.store.db.commit()
        except BaseException:
            self.store.db.rollback()
            raise
        return dict(self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?", (issue_id,)).fetchone())

    def _issue_event(self, issue_id, actor, event_type, from_status, to_status, payload=None, key=None):
        key = key or f"issue:{issue_id}:{event_type}:{uuid.uuid4().hex}"
        self.store.db.execute("INSERT OR IGNORE INTO tool_issue_events VALUES (?,?,?,?,?,?,?,?,?)", ("ievent_" + uuid.uuid4().hex, issue_id, event_type, actor, from_status, to_status, json.dumps(payload or {}, ensure_ascii=False), key, time.time()))

    def record_issue_pair(self, actor_member_id, issue_id, *, repair_version, environment, input_hash, before_outcome, after_outcome):
        row=self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?",(issue_id,)).fetchone()
        if not row: raise ValueError("tool_issue_not_found")
        if actor_member_id != self._tool(row["tool_id"])["owner_member_id"]: raise ValueError("tool_access_denied")
        allowed={"success","failure","unknown","cancelled"}
        if before_outcome not in allowed or after_outcome not in allowed: raise ValueError("invalid_pair_outcome")
        repair = self.store.db.execute("SELECT 1 FROM tool_versions WHERE tool_id=? AND version=?", (row["tool_id"], repair_version)).fetchone()
        if not repair or repair_version <= int(row["version"]): raise ValueError("tool_version_not_found")
        if not isinstance(input_hash, str) or not input_hash.strip(): raise ValueError("tool_pair_input_hash_required")
        normalized=json.dumps(environment or {},sort_keys=True,ensure_ascii=False)
        issue_environment = row["environment"] or "{}"
        if json.loads(issue_environment) != (environment or {}):
            raise ValueError("tool_pair_environment_mismatch")
        executions = self.store.db.execute("SELECT execution_id,version,status,input_hash,dry_run FROM tool_executions WHERE tool_id=? AND input_hash=? AND version IN (?,?) ORDER BY finished_at DESC", (row["tool_id"], input_hash, row["version"], repair_version)).fetchall()
        observed = {}
        execution_ids = {}
        for item in executions:
            if int(item["dry_run"]):
                continue
            observed.setdefault(int(item["version"]), item["status"])
            execution_ids.setdefault(int(item["version"]), item["execution_id"])
        expected = {row["version"]: before_outcome, repair_version: after_outcome}
        status_map = {"success": "success", "failure": "failed", "unknown": "unknown", "cancelled": "cancelled"}
        if any(version not in observed or observed[version] != status_map[outcome] for version, outcome in expected.items()):
            raise ValueError("tool_pair_execution_evidence_required")
        for version, execution_id in execution_ids.items():
            evidence = self.store.db.execute("SELECT environment, outcome, execution_id FROM tool_verifications WHERE tool_id=? AND version=? AND execution_id=?", (row["tool_id"], version, execution_id)).fetchall()
            if not any(json.loads(item["environment"] or "{}") == (environment or {}) and item["outcome"] == expected[version] for item in evidence):
                raise ValueError("tool_pair_environment_evidence_mismatch")
        self.store.db.execute("INSERT OR IGNORE INTO tool_issue_pairs (pair_id,issue_id,repair_version,environment,input_hash,before_outcome,after_outcome,created_at,before_execution_id,after_execution_id) VALUES (?,?,?,?,?,?,?,?,?,?)",("pair_"+uuid.uuid4().hex,issue_id,repair_version,normalized,input_hash,before_outcome,after_outcome,time.time(),execution_ids[row["version"]],execution_ids[repair_version]))
        return self.issue_statistics(actor_member_id,row["tool_id"],version=repair_version,environment=environment)

    def issue_statistics(self, actor_member_id, tool_id, *, version=None, environment=None):
        tool = self._tool(tool_id)
        if actor_member_id != tool["owner_member_id"] and not self.store.db.execute("SELECT 1 FROM tool_shares WHERE tool_id=? AND recipient_member_id=?", (tool_id, actor_member_id)).fetchone():
            raise ValueError("tool_access_denied")
        args=[tool_id]; sql="SELECT outcome, environment FROM tool_verifications WHERE tool_id=?"
        if version is not None: sql += " AND version=?"; args.append(version)
        rows=self.store.db.execute(sql,args).fetchall(); counts={"success":0,"failure":0,"unknown":0,"cancelled":0}
        for row in rows:
            if environment is not None and json.loads(row["environment"] or "{}") != environment: continue
            counts[row["outcome"]]=counts.get(row["outcome"],0)+1
        paired_sql="SELECT before_outcome,after_outcome FROM tool_issue_pairs WHERE issue_id IN (SELECT issue_id FROM tool_issues WHERE tool_id=?)"
        paired_args=[tool_id]
        if version is not None: paired_sql += " AND repair_version=?"; paired_args.append(version)
        pairs=self.store.db.execute(paired_sql,paired_args).fetchall()
        if environment is not None:
            env=json.dumps(environment or {},sort_keys=True,ensure_ascii=False)
            pairs=self.store.db.execute(paired_sql + " AND environment=?", paired_args+[env]).fetchall()
        comparable=[r for r in pairs if r["before_outcome"] in {"success","failure"} and r["after_outcome"] in {"success","failure"}]
        wins=sum(r["before_outcome"]=="failure" and r["after_outcome"]=="success" for r in comparable)
        losses=sum(r["before_outcome"]=="success" and r["after_outcome"]=="failure" for r in comparable)
        n=len(comparable)
        if n < 3:
            improvement_interval={"status":"unknown","metric":"paired_success_rate_difference","method":"insufficient_paired_samples","lower":None,"upper":None}
        else:
            # Difference of paired success rates: after success minus before success.
            # Discordant pairs identify the signed point estimate; concordant pairs keep the denominator honest.
            point=(wins-losses)/n
            variance=max(0.0, (wins+losses)/n - point*point) / n
            radius=1.96*math.sqrt(variance)
            improvement_interval={"status":"estimated","metric":"paired_success_rate_difference","confidence":0.95,"lower":max(-1.0, point-radius),"upper":min(1.0, point+radius)}
        return {"tool_id":tool_id,"version":version,"environment":environment or {},"counts":counts,"reliability_lower":wilson_lower(counts["success"],counts["failure"]),"sample_count":sum(counts.values()),"paired":{"n":len(comparable),"improved":wins,"regressed":losses,"net_improved":wins-losses,"improvement_confidence_interval":improvement_interval}}

    def triage_issue(self, actor_member_id, issue_id, *, status="triaged", reason="", due_seconds=86400):
        row=self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?",(issue_id,)).fetchone()
        if not row: raise ValueError("tool_issue_not_found")
        tool=self._tool(row["tool_id"])
        if actor_member_id != tool["owner_member_id"]: raise ValueError("tool_access_denied")
        allowed={"open":{"triaged","rejected"},"needs_information":{"triaged","rejected"},"reopened":{"triaged","rejected"},"triaged":{"repairing","rejected"}}
        if status not in allowed.get(row["status"],set()): raise ValueError("invalid_tool_issue_transition")
        if status=="rejected" and not reason.strip(): raise ValueError("tool_issue_rejection_reason_required")
        escalation_target = self.store.get_setting("dispatcher_member_id") if status == "rejected" else None
        if status == "rejected" and not escalation_target: raise ValueError("issue_dispatcher_not_configured")
        now=time.time()
        self.store.db.execute("BEGIN")
        try:
            self.store.db.execute("UPDATE tool_issues SET status=?,triage_owner_member_id=?,triage_reason=?,rejection_reason=?,escalation_to_member_id=?,escalated_at=?,due_at=?,updated_at=? WHERE issue_id=?",(status,actor_member_id,reason or None,reason if status=="rejected" else None,escalation_target,now if status=="rejected" else None,now+max(1,float(due_seconds)),now,issue_id))
            self._event(row["tool_id"],row["version"],"issue_triaged",{"issue_id":issue_id,"status":status,"reason":reason},f"issue:{issue_id}:triage:{status}")
            self._issue_event(issue_id, actor_member_id, "triaged", row["status"], status, {"reason":reason})
            self.store.db.commit()
        except BaseException:
            self.store.db.rollback()
            raise
        return dict(self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?",(issue_id,)).fetchone())

    def request_issue_information(self, actor_member_id, issue_id, *, request):
        row=self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?",(issue_id,)).fetchone()
        if not row: raise ValueError("tool_issue_not_found")
        if actor_member_id != self._tool(row["tool_id"])["owner_member_id"] or not request.strip(): raise ValueError("tool_access_denied" if not request.strip() else "tool_access_denied")
        if row["status"] not in {"open","triaged","repairing","needs_information"}: raise ValueError("invalid_tool_issue_transition")
        now=time.time(); self.store.db.execute("BEGIN")
        try:
            self.store.db.execute("UPDATE tool_issues SET status='needs_information',requested_information=?,updated_at=? WHERE issue_id=?",(request,now,issue_id)); self._event(row["tool_id"],row["version"],"issue_information_requested",{"issue_id":issue_id,"request":request},f"issue:{issue_id}:information:{uuid.uuid4().hex}"); self._issue_event(issue_id, actor_member_id, "information_requested", row["status"], "needs_information", {"request":request}); self.store.db.commit()
        except BaseException:
            self.store.db.rollback(); raise
        return dict(self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?",(issue_id,)).fetchone())

    def provide_issue_information(self, reporter_member_id, issue_id, *, information):
        row=self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?",(issue_id,)).fetchone()
        if not row: raise ValueError("tool_issue_not_found")
        if reporter_member_id != row["reporter_member_id"]: raise ValueError("tool_issue_reporter_required")
        if row["status"] != "needs_information" or not information.strip(): raise ValueError("tool_issue_information_not_requested")
        now=time.time(); self.store.db.execute("BEGIN")
        try:
            self.store.db.execute("UPDATE tool_issues SET status='open',requested_information=NULL,description=description || char(10) || 'Reporter information: ' || ?,updated_at=? WHERE issue_id=?",(information,now,issue_id)); self._issue_event(issue_id, reporter_member_id, "information_provided", row["status"], "open", {"information_hash": hashlib.sha256(information.encode()).hexdigest()}, f"issue:{issue_id}:information-provided:{uuid.uuid4().hex}")
            self._event(row["tool_id"],row["version"],"issue_information_provided",{"issue_id":issue_id},f"issue:{issue_id}:information-provided:{uuid.uuid4().hex}"); self.store.db.commit()
        except BaseException:
            self.store.db.rollback(); raise
        return dict(self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?",(issue_id,)).fetchone())

    def link_duplicate_issue(self, actor_member_id, issue_id, duplicate_of):
        row=self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?",(issue_id,)).fetchone(); parent=self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?",(duplicate_of,)).fetchone()
        if not row or not parent: raise ValueError("tool_issue_not_found")
        if actor_member_id != self._tool(row["tool_id"])["owner_member_id"]: raise ValueError("tool_access_denied")
        if row["tool_id"] != parent["tool_id"] or issue_id == duplicate_of: raise ValueError("invalid_duplicate_issue")
        if row["duplicate_of"]: return dict(row)
        cursor = duplicate_of
        seen = set()
        while cursor:
            if cursor in seen or cursor == issue_id:
                raise ValueError("duplicate_issue_cycle")
            seen.add(cursor)
            linked = self.store.db.execute("SELECT duplicate_of FROM tool_issues WHERE issue_id=?", (cursor,)).fetchone()
            cursor = linked["duplicate_of"] if linked else None
        now=time.time(); self.store.db.execute("UPDATE tool_issues SET duplicate_of=?,updated_at=? WHERE issue_id=?",(duplicate_of,now,issue_id))
        self._event(row["tool_id"],row["version"],"issue_duplicate_linked",{"issue_id":issue_id,"duplicate_of":duplicate_of},f"issue:{issue_id}:duplicate:{duplicate_of}")
        return dict(self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?",(issue_id,)).fetchone())

    def escalate_issue(self, actor_member_id, issue_id, *, reason="timeout", now=None):
        row=self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?",(issue_id,)).fetchone()
        if not row: raise ValueError("tool_issue_not_found")
        if actor_member_id != self._tool(row["tool_id"])["owner_member_id"]: raise ValueError("tool_access_denied")
        if row["status"] in {"resolved","rejected"}: raise ValueError("tool_issue_not_escalatable")
        stamp=time.time() if now is None else float(now)
        target = self.store.get_setting("dispatcher_member_id")
        if not target:
            raise ValueError("issue_dispatcher_not_configured")
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            self.store.db.execute("UPDATE tool_issues SET escalated_at=?,escalation_to_member_id=?,updated_at=? WHERE issue_id=?",(stamp,target,stamp,issue_id))
            work_item_id = "escalation_" + uuid.uuid4().hex
            existing = self.store.db.execute("SELECT * FROM tool_issue_escalations WHERE issue_id=? AND dispatcher_member_id=? AND reason=? AND status IN ('open','claimed') ORDER BY created_at DESC LIMIT 1", (issue_id, target, reason)).fetchone()
            if existing:
                work_item_id = existing["work_item_id"]
            else:
                self.store.db.execute("INSERT INTO tool_issue_escalations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (work_item_id, issue_id, target, "open", reason, None, None, stamp, None, None, stamp))
                self.store.db.execute("INSERT INTO tool_issue_escalation_events VALUES (?,?,?,?,?,?,?)", ("eevent_" + uuid.uuid4().hex, work_item_id, "created", actor_member_id, json.dumps({"issue_id": issue_id, "reason": reason}, ensure_ascii=False), f"escalation:{work_item_id}:created", stamp))
            self._event(row["tool_id"],row["version"],"issue_escalated",{"issue_id":issue_id,"reason":reason,"work_item_id":work_item_id,"dispatcher_member_id":target},f"issue:{issue_id}:escalate:{int(stamp)}")
            result = {**dict(self.store.db.execute("SELECT * FROM tool_issues WHERE issue_id=?",(issue_id,)).fetchone()), "work_item_id": work_item_id}
            self.store.db.commit()
            return result
        except BaseException:
            self.store.db.rollback()
            raise

    def list_issue_escalations(self, dispatcher_member_id, *, status=None):
        args=[dispatcher_member_id]
        sql="SELECT * FROM tool_issue_escalations WHERE dispatcher_member_id=?"
        if status is not None:
            sql += " AND status=?"; args.append(status)
        sql += " ORDER BY created_at"
        return [dict(row) for row in self.store.db.execute(sql, args).fetchall()]

    def claim_issue_escalation(self, dispatcher_member_id, work_item_id):
        now=time.time()
        row=self.store.db.execute("SELECT * FROM tool_issue_escalations WHERE work_item_id=?", (work_item_id,)).fetchone()
        if not row: raise ValueError("tool_issue_escalation_not_found")
        if row["dispatcher_member_id"] != dispatcher_member_id: raise ValueError("tool_issue_escalation_forbidden")
        if row["status"] != "open": raise ValueError("tool_issue_escalation_not_open")
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            updated = self.store.db.execute("UPDATE tool_issue_escalations SET status='claimed',claimed_by=?,claimed_at=?,updated_at=? WHERE work_item_id=? AND status='open'", (dispatcher_member_id, now, now, work_item_id)).rowcount
            if updated != 1:
                self.store.db.rollback()
                raise ValueError("tool_issue_escalation_not_open")
            self.store.db.execute("INSERT INTO tool_issue_escalation_events VALUES (?,?,?,?,?,?,?)", ("eevent_" + uuid.uuid4().hex, work_item_id, "claimed", dispatcher_member_id, "{}", f"escalation:{work_item_id}:claimed", now))
            result = dict(self.store.db.execute("SELECT * FROM tool_issue_escalations WHERE work_item_id=?", (work_item_id,)).fetchone())
            self.store.db.commit()
            return result
        except BaseException:
            self.store.db.rollback()
            raise

    def resolve_issue_escalation(self, dispatcher_member_id, work_item_id, *, status, resolution=""):
        if status not in {"completed", "rejected"}: raise ValueError("invalid_tool_issue_escalation_status")
        now=time.time(); row=self.store.db.execute("SELECT * FROM tool_issue_escalations WHERE work_item_id=?", (work_item_id,)).fetchone()
        if not row: raise ValueError("tool_issue_escalation_not_found")
        if row["dispatcher_member_id"] != dispatcher_member_id or row["claimed_by"] != dispatcher_member_id: raise ValueError("tool_issue_escalation_forbidden")
        if row["status"] != "claimed": raise ValueError("tool_issue_escalation_not_claimed")
        if status == "rejected" and not resolution.strip(): raise ValueError("tool_issue_escalation_rejection_reason_required")
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            updated = self.store.db.execute("UPDATE tool_issue_escalations SET status=?,resolution=?,completed_at=?,updated_at=? WHERE work_item_id=? AND status='claimed'", (status, resolution, now, now, work_item_id)).rowcount
            if updated != 1:
                self.store.db.rollback()
                raise ValueError("tool_issue_escalation_not_claimed")
            self.store.db.execute("INSERT INTO tool_issue_escalation_events VALUES (?,?,?,?,?,?,?)", ("eevent_" + uuid.uuid4().hex, work_item_id, status, dispatcher_member_id, json.dumps({"resolution": resolution}, ensure_ascii=False), f"escalation:{work_item_id}:{status}", now))
            result = dict(self.store.db.execute("SELECT * FROM tool_issue_escalations WHERE work_item_id=?", (work_item_id,)).fetchone())
            issue = self.store.db.execute("SELECT status FROM tool_issues WHERE issue_id=?", (row["issue_id"],)).fetchone()
            # Dispatcher completion records handling of the work item; only the reporter's verified repair confirmation resolves the issue.
            result["issue_status"] = issue["status"] if issue else None
            result["issue_resolution_required"] = True
            if status == "rejected":
                next_item_id = "escalation_" + uuid.uuid4().hex
                self.store.db.execute("INSERT INTO tool_issue_escalations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (next_item_id, row["issue_id"], row["dispatcher_member_id"], "open", "re-escalated", None, None, now, None, None, now))
                payload = json.dumps({"previous_work_item_id": work_item_id, "reason": resolution}, ensure_ascii=False)
                self.store.db.execute("INSERT INTO tool_issue_escalation_events VALUES (?,?,?,?,?,?,?)", ("eevent_" + uuid.uuid4().hex, next_item_id, "created", dispatcher_member_id, payload, f"escalation:{next_item_id}:created", now))
                self.store.db.execute("INSERT INTO tool_issue_escalation_events VALUES (?,?,?,?,?,?,?)", ("eevent_" + uuid.uuid4().hex, work_item_id, "re-escalated", dispatcher_member_id, json.dumps({"next_work_item_id": next_item_id}, ensure_ascii=False), f"escalation:{work_item_id}:re-escalated", now))
                result["next_work_item_id"] = next_item_id
            self.store.db.commit()
            return result
        except BaseException:
            self.store.db.rollback()
            raise

    def expire_issue_escalations(self, actor_member_id=None, *, now=None):
        stamp=time.time() if now is None else float(now)
        sql="SELECT i.issue_id, t.owner_member_id FROM tool_issues i JOIN tool_assets t ON t.tool_id=i.tool_id WHERE i.due_at IS NOT NULL AND i.due_at<=? AND i.escalated_at IS NULL AND i.status NOT IN ('resolved','rejected')"
        args=[stamp]
        if actor_member_id is not None:
            sql += " AND i.triage_owner_member_id=?"; args.append(actor_member_id)
        due=self.store.db.execute(sql,args).fetchall()
        return [self.escalate_issue(r["owner_member_id"], r["issue_id"], reason="timeout", now=stamp) for r in due]

    def open_circuit_breaker(self, actor_member_id, tool_id, version, *, scope="version", reason="risk"):
        if actor_member_id != self._tool(tool_id)["owner_member_id"]: raise ValueError("tool_access_denied")
        now=time.time(); self.store.db.execute("INSERT OR REPLACE INTO tool_circuit_breakers VALUES (?,?,?,?,?,?,?,?)",(tool_id,version,scope,reason,"open",now,None,None)); self._event(tool_id,version,"circuit_breaker_opened",{"scope":scope,"reason":reason},f"breaker:{tool_id}:{version}:{scope}:open:{uuid.uuid4().hex}"); return dict(self.store.db.execute("SELECT * FROM tool_circuit_breakers WHERE tool_id=? AND version=? AND scope=?",(tool_id,version,scope)).fetchone())

    def recover_circuit_breaker(self, actor_member_id, tool_id, version, *, scope="version"):
        if actor_member_id != self._tool(tool_id)["owner_member_id"]: raise ValueError("tool_access_denied")
        verified = self.store.db.execute("SELECT 1 FROM tool_verifications WHERE tool_id=? AND version=? AND outcome='success' AND execution_id IS NOT NULL AND evidence_ids NOT IN ('[]', '') ORDER BY created_at DESC LIMIT 1", (tool_id, version)).fetchone()
        bad = self.store.db.execute("SELECT 1 FROM tool_verifications WHERE tool_id=? AND version=? AND outcome IN ('failure','unknown','cancelled')", (tool_id, version)).fetchone()
        if not verified or bad:
            raise ValueError("tool_circuit_breaker_recovery_not_verified")
        now=time.time(); cur=self.store.db.execute("UPDATE tool_circuit_breakers SET status='recovered',recovered_at=?,recovered_by=? WHERE tool_id=? AND version=? AND scope=? AND status='open'",(now,actor_member_id,tool_id,version,scope))
        if cur.rowcount != 1: raise ValueError("tool_circuit_breaker_not_open")
        self._event(tool_id,version,"circuit_breaker_recovered",{"scope":scope},f"breaker:{tool_id}:{version}:{scope}:recovered:{uuid.uuid4().hex}"); return dict(self.store.db.execute("SELECT * FROM tool_circuit_breakers WHERE tool_id=? AND version=? AND scope=?",(tool_id,version,scope)).fetchone())

    def grant_host_authorization(self, owner_member_id: str, tool_id: str, version: int, *, capabilities: list[str], reason: str) -> dict[str, Any]:
        if owner_member_id != self._tool(tool_id)["owner_member_id"]:
            raise ValueError("tool_access_denied")
        allowed = {"filesystem.read", "filesystem.write", "network", "process.spawn", "application.control", "device", "package.install"}
        if not isinstance(capabilities, list) or not capabilities or any(item not in allowed for item in capabilities) or not reason.strip():
            raise ValueError("host_authorization_invalid")
        if not self.store.db.execute("SELECT 1 FROM tool_versions WHERE tool_id=? AND version=?", (tool_id, version)).fetchone():
            raise ValueError("tool_version_not_found")
        now = time.time()
        authorization_id = "hauth_" + uuid.uuid4().hex
        self.store.db.execute("INSERT INTO host_authorizations VALUES (?,?,?,?,?,?,?,?,?,?)", (authorization_id, owner_member_id, tool_id, version, json.dumps(sorted(set(capabilities))), "active", reason, now, None, None))
        self._event(tool_id, version, "host_authorization_granted", {"authorization_id": authorization_id, "capabilities": sorted(set(capabilities))}, f"host-auth:{authorization_id}:grant")
        return dict(self.store.db.execute("SELECT * FROM host_authorizations WHERE authorization_id=?", (authorization_id,)).fetchone())

    def revoke_host_authorization(self, owner_member_id: str, authorization_id: str, *, reason: str) -> dict[str, Any]:
        row = self.store.db.execute("SELECT * FROM host_authorizations WHERE authorization_id=?", (authorization_id,)).fetchone()
        if not row or row["owner_member_id"] != owner_member_id:
            raise ValueError("host_authorization_not_found")
        if not reason.strip():
            raise ValueError("host_revoke_reason_required")
        updated = self.store.db.execute("UPDATE host_authorizations SET status='revoked', revoked_at=?, revoke_reason=? WHERE authorization_id=? AND status='active'", (time.time(), reason, authorization_id)).rowcount
        if updated != 1:
            raise ValueError("host_authorization_not_active")
        self._event(row["tool_id"], row["version"], "host_authorization_revoked", {"authorization_id": authorization_id, "reason": reason}, f"host-auth:{authorization_id}:revoke")
        return dict(self.store.db.execute("SELECT * FROM host_authorizations WHERE authorization_id=?", (authorization_id,)).fetchone())

    def cancel_execution(self, actor_member_id: str, execution_id: str, *, reason: str) -> dict[str, Any]:

        row = self.store.db.execute("SELECT e.*, a.owner_member_id FROM tool_executions e JOIN tool_assets a ON a.tool_id=e.tool_id WHERE e.execution_id=?", (execution_id,)).fetchone()
        if not row or actor_member_id not in {row["actor_member_id"], row["owner_member_id"]}:
            raise ValueError("execution_access_denied")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("execution_cancel_reason_required")
        with self._active_executions_lock:
            active = self._active_executions.get(execution_id)
            if not active:
                current = self.store.db.execute("SELECT status FROM tool_executions WHERE execution_id=?", (execution_id,)).fetchone()
                return {"execution_id": execution_id, "status": current["status"] if current else "unknown"}
            active["cancel_requested"] = True
            proc = active["process"]
            self.store.db.execute("UPDATE tool_executions SET status='cancelling', termination_reason=? WHERE execution_id=? AND status='running'", (reason, execution_id))
        _terminate_process_tree(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            status = "unknown"
        else:
            status = "cancelled" if proc.poll() is not None else "unknown"
        self._event(row["tool_id"], row["version"], "execution_cancel_requested", {"execution_id": execution_id, "actor_member_id": actor_member_id, "reason": reason, "status": status}, f"tool:execution:{execution_id}:cancel")
        return {"execution_id": execution_id, "status": status}

    def list_running_executions(self, actor_member_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.store.db.execute("SELECT execution_id, tool_id, version, actor_member_id, task_id, status, started_at FROM tool_executions WHERE status IN ('running','cancelling') AND (actor_member_id=? OR tool_id IN (SELECT tool_id FROM tool_assets WHERE owner_member_id=?)) ORDER BY started_at", (actor_member_id, actor_member_id))]

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



    def propose(self, owner_member_id: str, name: str, description: str, *, reuse_lower: float, manual_cost_upper: float, call_cost_lower: float, build_cost_upper: float, maintenance_cost_upper: float, risk_cost_upper: float, manifest: dict[str, Any] | None = None, manual_cost_lower: float | None = None, call_cost_upper: float | None = None, cost_unit: str | None = None) -> dict[str, Any]:

        self._member_dir(owner_member_id)

        values = {k: _finite(v, k, nonnegative=True) for k, v in {"reuse_lower": reuse_lower, "manual_cost_upper": manual_cost_upper, "call_cost_lower": call_cost_lower, "build_cost_upper": build_cost_upper, "maintenance_cost_upper": maintenance_cost_upper, "risk_cost_upper": risk_cost_upper}.items()}

        conservative = manual_cost_lower is not None and call_cost_upper is not None and isinstance(cost_unit, str) and bool(cost_unit.strip())

        if (manual_cost_lower is None) != (call_cost_upper is None):

            raise ValueError("incomplete_conservative_cost_bounds")

        if (manual_cost_lower is not None or call_cost_upper is not None) and (not isinstance(cost_unit, str) or not cost_unit.strip()):

            raise ValueError("cost_unit_required")

        if conservative:

            manual_lower = _finite(manual_cost_lower, "manual_cost_lower", nonnegative=True)

            call_upper = _finite(call_cost_upper, "call_cost_upper", nonnegative=True)

            per_call_saving_lower = manual_lower - call_upper

            benefit_lower = values["reuse_lower"] * per_call_saving_lower - values["build_cost_upper"] - values["maintenance_cost_upper"] - values["risk_cost_upper"]

        else:

            manual_lower = call_upper = per_call_saving_lower = benefit_lower = None

        proposal = {**values, "manual_cost_lower": manual_lower, "call_cost_upper": call_upper, "cost_unit": cost_unit.strip() if conservative else None, "legacy_cost_bounds": not conservative, "per_call_saving_lower": per_call_saving_lower, "benefit_lower": benefit_lower, "gate": "pass" if conservative and benefit_lower > 0 else "unverified" if not conservative else "reject", "manifest": manifest or {}}

        if conservative and benefit_lower <= 0:

            return {"status": "rejected", "reason": "insufficient_reuse_value", "proposal": proposal}

        now = time.time(); tool_id = f"tool_{uuid.uuid4().hex}"

        self.store.db.execute("INSERT INTO tool_assets VALUES (?,?,?,?,?,?,?,?,?)", (tool_id, owner_member_id, name, description, "draft", json.dumps(proposal, ensure_ascii=False), now, now, 0))

        self._event(tool_id, None, "proposed", proposal, f"tool:{tool_id}:proposed")

        return {"status": "draft", "tool_id": tool_id, "proposal": proposal}



    def add_version(self, owner_member_id: str, tool_id: str, content: bytes | str, manifest: dict[str, Any]) -> dict[str, Any]:

        with self.store._write_lock:
            return self._add_version_locked(owner_member_id, tool_id, content, manifest)

    def _add_version_locked(self, owner_member_id: str, tool_id: str, content: bytes | str, manifest: dict[str, Any]) -> dict[str, Any]:
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

        manifest = {**manifest, "content_hash": digest}

        canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

        manifest_hash = hashlib.sha256(canonical).hexdigest()

        manifest["manifest_hash"] = manifest_hash

        duplicates = self.store.db.execute("SELECT tool_id, version, manifest FROM tool_versions WHERE tool_id=? AND content_hash=?", (tool_id, digest)).fetchall()

        for duplicate in duplicates:

            if json.loads(duplicate["manifest"]).get("manifest_hash") == manifest_hash:

                return {"tool_id": tool_id, "version": duplicate["version"], "content_hash": digest, "manifest_hash": manifest_hash, "status": "idempotent"}

        version = self.store.db.execute("SELECT COALESCE(MAX(version),0)+1 FROM tool_versions WHERE tool_id=?", (tool_id,)).fetchone()[0]

        tool_root = self._member_dir(owner_member_id) / tool_id

        tool_root.mkdir(parents=True, exist_ok=True)

        path = tool_root / f"v{version}"

        staging = tool_root / f".v{version}.{uuid.uuid4().hex}.staging"

        staging.mkdir(parents=False, exist_ok=False)

        entrypoint = manifest.get("entrypoint") or ["python", "tool.py"]
        source_names = {"python": "tool.py", "node": "tool.js", "powershell": "tool.ps1"}
        if not isinstance(entrypoint, list) or len(entrypoint) != 2 or entrypoint[0] not in source_names or entrypoint[1] != source_names[entrypoint[0]]:
            raise ValueError("tool_entrypoint_not_allowlisted")
        source = staging / source_names[entrypoint[0]]

        try:
            with source.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            now = time.time()

            os.replace(staging, path)
            source = path / source_names[entrypoint[0]]
            self._insert_version_row(tool_id, version, digest, source, manifest, now)
        except BaseException:
            if staging.exists():
                self._quarantine_version_path(tool_id, version, staging)
            elif path.exists() and not self.store.db.execute("SELECT 1 FROM tool_versions WHERE tool_id=? AND version=?", (tool_id, version)).fetchone():
                self._quarantine_version_path(tool_id, version, path)
            raise

        # current_version is the stable execution target. Candidate versions are

        # selected explicitly by the owner for verification and promoted atomically.

        if tool["current_version"] == 0:

            # Keep proposal economics on the asset; version manifest belongs to

            # tool_versions and must not erase conservative admission evidence.

            self.store.db.execute("UPDATE tool_assets SET current_version=?, updated_at=? WHERE tool_id=?", (version, now, tool_id))

        self._event(tool_id, version, "version_added", {"content_hash": digest}, f"tool:{tool_id}:version:{version}")

        return {"tool_id": tool_id, "version": version, "content_hash": digest, "status": "draft"}



    def _insert_version_row(self, tool_id: str, version: int, digest: str, source: Path, manifest: dict[str, Any], created_at: float) -> None:
        self.store.db.execute("INSERT INTO tool_versions VALUES (?,?,?,?,?,?)", (tool_id, version, digest, str(source), json.dumps(manifest, ensure_ascii=False), created_at))

    def _quarantine_version_path(self, tool_id: str, version: int, path: Path) -> None:
        quarantine = path.parent / ".orphaned"
        quarantine.mkdir(exist_ok=True)
        target = quarantine / path.name
        if target.exists():
            target = quarantine / f"{path.name}-{uuid.uuid4().hex}"
        os.replace(path, target)
        self.recovery_events.append({"tool_id": tool_id, "version": version, "path": str(target), "action": "quarantined_orphan"})

    def verify(self, owner_member_id: str, tool_id: str, version: int, outcome: str, *, independent_key: str, task_id: str | None = None, environment: dict[str, Any] | None = None, evidence_ids: list[str] | None = None, execution_id: str | None = None, input_hash: str | None = None, expected_output: Any = None) -> dict[str, Any]:
        tool = self._tool(tool_id)
        if tool["owner_member_id"] != owner_member_id:
            raise ValueError("tool_access_denied")
        if outcome not in {"success", "failure", "unknown", "cancelled"}:
            raise ValueError("invalid_tool_outcome")
        if not self.store.db.execute("SELECT 1 FROM tool_versions WHERE tool_id=? AND version=?", (tool_id, version)).fetchone():
            raise ValueError("tool_version_not_found")
        if not isinstance(independent_key, str) or not independent_key.strip():
            raise ValueError("verification_key_required")
        if not isinstance(evidence_ids, list) or not evidence_ids or any(not isinstance(item, str) or not item.strip() for item in evidence_ids):
            raise ValueError("verification_evidence_required")
        if outcome == "success" and (execution_id is None or input_hash is None or expected_output is None):
            raise ValueError("verification_success_binding_required")
        if execution_id is not None:
            execution = self.store.db.execute("SELECT tool_id, version, actor_member_id, input_hash, status, stdout FROM tool_executions WHERE execution_id=?", (execution_id,)).fetchone()
            if not execution or execution["tool_id"] != tool_id or execution["version"] != version:
                raise ValueError("verification_execution_not_found")
            if execution["actor_member_id"] != owner_member_id:
                raise ValueError("verification_execution_actor_mismatch")
            if input_hash != execution["input_hash"]:
                raise ValueError("verification_input_binding_mismatch")
            if outcome == "success" and execution["status"] != "success":
                raise ValueError("verification_execution_outcome_mismatch")
            if outcome == "success":
                manifest = json.loads(self.store.db.execute("SELECT manifest FROM tool_versions WHERE tool_id=? AND version=?", (tool_id, version)).fetchone()["manifest"])
                recorded = execution["stdout"] if manifest.get("output_contract", {}).get("type") in {"text", "string"} else json.loads(execution["stdout"])
                if recorded != expected_output:
                    raise ValueError("verification_expected_output_mismatch")
        env_json = json.dumps(environment or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        evidence_json = json.dumps(evidence_ids, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected_json = json.dumps(expected_output, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if expected_output is not None else None
        existing = self.store.db.execute("SELECT * FROM tool_verifications WHERE tool_id=? AND version=? AND independent_key=?", (tool_id, version, independent_key)).fetchone()
        if existing:
            same = (existing["outcome"], existing["task_id"], existing["environment"], existing["evidence_ids"], existing["execution_id"], existing["input_hash"], existing["expected_output"]) == (outcome, task_id, env_json, evidence_json, execution_id, input_hash, expected_json)
            if not same:
                raise ValueError("verification_idempotency_conflict")
            return dict(existing)
        now = time.time(); verification_id = f"tverify_{uuid.uuid4().hex}"
        self.store.db.execute("BEGIN")
        try:
            self.store.db.execute("INSERT INTO tool_verifications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (verification_id, tool_id, version, task_id, env_json, outcome, evidence_json, independent_key, execution_id, input_hash, expected_json, now))
            stats = self.stats(tool_id, version)
            self._event(tool_id, version, "verification", {"outcome": outcome, "stats": stats}, f"tool:{tool_id}:verification:{independent_key}")
            version_manifest = json.loads(self.store.db.execute("SELECT manifest FROM tool_versions WHERE tool_id=? AND version=?", (tool_id, version)).fetchone()["manifest"])
            supply_chain_ok = version_manifest.get("source_kind", "internal") == "internal" or (version_manifest.get("source") and version_manifest.get("license") and version_manifest.get("scan_status") == "passed")
            gate = self._admission_gate(tool, version, version_manifest, stats)
            if gate["eligible"] and supply_chain_ok and tool["status"] in {"draft", "candidate"}:
                self._transition(tool_id, "active", {"version": version, "stats": stats, "admission": gate}, f"tool:{tool_id}:activate:{version}")
                self.store.db.execute("UPDATE tool_assets SET current_version=?, updated_at=? WHERE tool_id=?", (version, time.time(), tool_id))
            elif tool["status"] == "draft":
                self._transition(tool_id, "candidate", {"version": version}, f"tool:{tool_id}:candidate:{version}")
            self.store.db.commit()
        except BaseException:
            self.store.db.rollback()
            raise
        return {"verification_id": verification_id, "stats": stats, "status": self._tool(tool_id)["status"]}

    def trial(self, recipient_member_id: str, share_id: str, outcome: str, *, independent_key: str, task_id: str | None = None, environment: dict[str, Any] | None = None, evidence_ids: list[str] | None = None, execution_id: str | None = None, input_hash: str | None = None, expected_output: Any = None) -> dict[str, Any]:
        row = self.store.db.execute("SELECT * FROM tool_shares WHERE share_id=? AND recipient_member_id=?", (share_id, recipient_member_id)).fetchone()
        if not row or row["status"] not in {"trial", "active"}:
            raise ValueError("tool_share_not_in_trial")
        if outcome not in {"success", "failure", "unknown", "cancelled"}:
            raise ValueError("invalid_tool_outcome")
        if not isinstance(evidence_ids, list) or not evidence_ids or any(not isinstance(item, str) or not item.strip() for item in evidence_ids):
            raise ValueError("verification_evidence_required")
        if outcome == "success" and (execution_id is None or input_hash is None or expected_output is None):
            raise ValueError("trial_success_requires_execution_binding")
        if execution_id is not None:
            execution = self.store.db.execute("SELECT * FROM tool_executions WHERE execution_id=?", (execution_id,)).fetchone()
            if not execution or (execution["tool_id"], execution["version"], execution["actor_member_id"]) != (row["tool_id"], row["version"], recipient_member_id):
                raise ValueError("trial_execution_mismatch")
            if input_hash != execution["input_hash"]:
                raise ValueError("trial_input_binding_mismatch")
            if outcome == "success" and execution["status"] != "success":
                raise ValueError("trial_execution_outcome_mismatch")
            if outcome == "success" and execution["stdout"] != expected_output:
                raise ValueError("trial_expected_output_mismatch")
        result = self._record_trial(row, recipient_member_id, outcome, independent_key, task_id, environment, evidence_ids, execution_id=execution_id, input_hash=input_hash, expected_output=expected_output)
        return {"share_id": share_id, "outcome": outcome, "status": "trial", "verification": result}

    def list(self, owner_member_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        self._member_dir(owner_member_id)
        sql = "SELECT * FROM tool_assets WHERE owner_member_id=?"
        args: list[Any] = [owner_member_id]
        if status:
            sql += " AND status=?"
            args.append(status)
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

    def context_recall_report(self, member_id: str, instruction: str, *, limit: int = 20) -> dict[str, Any]:
        """Run candidate recall, then apply access/version/reliability admission."""
        if limit < 0:
            raise ValueError("invalid_tool_limit")
        query = instruction.casefold().strip()
        # Preserve Chinese compound terms: replacing single-character stop words
        # would corrupt terms such as "通用分析" ("用" is part of the term).
        stop_phrases = ("请帮我", "请用", "帮我", "帮忙", "查询", "处理", "执行", "进行", "这份", "这个", "一下", "请")
        for phrase in stop_phrases:
            query = query.replace(phrase, " ")
        query_terms = {part.casefold() for part in query.split() if len(part.strip()) >= 2}
        chinese_spans = []
        current = []
        for char in query:
            if "\u4e00" <= char <= "\u9fff":
                current.append(char)
            elif current:
                chinese_spans.append("".join(current))
                current = []
        if current:
            chinese_spans.append("".join(current))
        query_terms.update(span for span in chinese_spans if len(span) >= 2)
        rows = self.store.db.execute(
            "SELECT * FROM tool_assets WHERE (owner_member_id=? OR tool_id IN (SELECT tool_id FROM tool_shares WHERE recipient_member_id=?))",
            (member_id, member_id),
        ).fetchall()
        candidates, accepted, rejected = [], [], []
        now = time.time()
        for row in rows:
            own = row["owner_member_id"] == member_id
            reject_reason = None
            if row["status"] != "active":
                reject_reason = "tool_not_active"
            if not own:
                share = self.store.db.execute(
                    "SELECT * FROM tool_shares WHERE tool_id=? AND version=? AND recipient_member_id=?",
                    (row["tool_id"], row["current_version"], member_id),
                ).fetchone()
                if not share or share["status"] not in {"trial", "active"}:
                    reject_reason = "share_not_active"
                elif share["permission"] not in {"team_readonly", "team_execute"}:
                    reject_reason = "share_permission_denied"
                elif share["expires_at"] is not None and share["expires_at"] <= now:
                    reject_reason = "share_expired"
            manifest = json.loads(row["manifest"] or "{}")
            contract = {key: manifest.get(key) for key in ("input_contract", "output_contract", "permissions", "failure_conditions")}
            text = " ".join((row["name"], row["description"], json.dumps(contract, ensure_ascii=False))).casefold()
            matched_by = sorted({term for term in query_terms if term in text})
            if len(query) >= 2 and query in text:
                matched_by.append(query)
            matched_by = sorted(set(matched_by))
            score = len(matched_by)
            card = {"tool_id": row["tool_id"], "name": row["name"], "description": row["description"], "version": row["current_version"], **contract, "matched_by": matched_by, "confidence": min(1.0, score / max(1, len(query_terms))), "permission": "owner_execute" if own else "team_readonly"}
            if not own:
                share = self.store.db.execute("SELECT share_id, permission FROM tool_shares WHERE tool_id=? AND version=? AND recipient_member_id=? AND status IN ('trial','active')", (row["tool_id"], row["current_version"], member_id)).fetchone()
                if share:
                    card["permission"] = share["permission"]
                    if share["permission"] == "team_execute":
                        card["share_id"] = share["share_id"]
            candidate = {"card": card, "score": score, "owner": own}
            candidates.append(candidate)
            if reject_reason is None and score == 0:
                reject_reason = "no_relevant_match"
            if reject_reason is None:
                breaker = self.store.db.execute("SELECT 1 FROM tool_circuit_breakers WHERE tool_id=? AND version=? AND status='open'", (row["tool_id"], row["current_version"])).fetchone()
                if breaker:
                    reject_reason = "circuit_breaker_open"
                else:
                    stats = self.stats(row["tool_id"], row["current_version"])
                    if sum(stats[key] for key in ("failures", "unknown", "cancelled")) > 0:
                        reject_reason = "reliability_not_clean"
            if reject_reason:
                rejected.append({"tool_id": row["tool_id"], "version": row["current_version"], "reason": reject_reason, "matched_by": matched_by})
            else:
                accepted.append(candidate)
        accepted.sort(key=lambda item: (-item["score"], 0 if item["owner"] else 1, item["card"]["tool_id"]))
        return {"candidates": candidates, "accepted": [item["card"] for item in accepted[:limit]], "rejected": rejected}

    def context_cards(self, member_id: str, instruction: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return deterministic, contract-only cards visible after two-stage admission."""
        cards = self.context_recall_report(member_id, instruction, limit=limit)["accepted"]
        return [{key: value for key, value in card.items() if key not in {"matched_by", "confidence"}} for card in cards]

    def stats(self, tool_id: str, version: int):
        rows = self.store.db.execute("SELECT outcome, task_id, environment, execution_id, evidence_ids FROM tool_verifications WHERE tool_id=? AND version=?", (tool_id, version)).fetchall()
        counts = {key: sum(row["outcome"] == key for row in rows) for key in ("success", "failure", "unknown", "cancelled")}
        successes = [row for row in rows if row["outcome"] == "success"]
        environments = {json.dumps(json.loads(row["environment"] or "{}"), sort_keys=True, separators=(",", ":")) for row in successes}
        tasks = {row["task_id"] for row in successes if row["task_id"]}
        bound = sum(bool(row["execution_id"] and json.loads(row["evidence_ids"] or "[]")) for row in successes)
        return {"successes": counts["success"], "failures": counts["failure"], "unknown": counts["unknown"], "cancelled": counts["cancelled"], "wilson_lower": wilson_lower(counts["success"], counts["failure"]), "environment_count": len(environments), "independent_task_count": len(tasks), "evidence_bound_successes": bound}

    def _admission_gate(self, tool, version: int, manifest: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
        proposal = tool["manifest"] if isinstance(tool["manifest"], dict) else json.loads(tool["manifest"] or "{}")
        cost_verified = not proposal.get("legacy_cost_bounds", True) and bool(proposal.get("cost_unit")) and proposal.get("benefit_lower") is not None and proposal["benefit_lower"] > 0
        risk = manifest.get("risk_level", "medium")
        thresholds = {"low": (3, 2, 2, 0.50), "medium": (5, 2, 3, 0.70), "high": (8, 3, 4, 0.90), "critical": (12, 4, 6, 0.97)}
        if risk not in thresholds: raise ValueError("invalid_tool_risk_level")
        min_successes, min_environments, min_tasks, min_wilson = thresholds[risk]
        clean = stats["failures"] == 0 and stats["unknown"] == 0 and stats["cancelled"] == 0
        eligible = cost_verified and clean and stats["successes"] >= min_successes and stats["environment_count"] >= min_environments and stats["independent_task_count"] >= min_tasks and stats["evidence_bound_successes"] >= min_successes and stats["wilson_lower"] >= min_wilson
        return {"eligible": eligible, "risk_level": risk, "cost_verified": cost_verified, "minimum_successes": min_successes, "minimum_environments": min_environments, "minimum_independent_tasks": min_tasks, "minimum_wilson_lower": min_wilson, "snapshot_version": 1, "stats": stats}

    def _record_trial(self, share, actor_member_id, outcome, independent_key, task_id, environment, evidence_ids, *, execution_id=None, input_hash=None, expected_output=None):

        now = time.time()

        key = f"share:{share['share_id']}:{independent_key}"

        existing = self.store.db.execute("SELECT * FROM tool_verifications WHERE tool_id=? AND version=? AND independent_key=?", (share["tool_id"], share["version"], key)).fetchone()

        env_json = json.dumps(environment or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

        evidence_json = json.dumps(evidence_ids, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

        if existing:

            same = existing["outcome"] == outcome and existing["task_id"] == task_id and existing["environment"] == env_json and existing["evidence_ids"] == evidence_json

            if not same:

                raise ValueError("verification_idempotency_conflict")

            return dict(existing)

        expected_json = json.dumps(expected_output, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if expected_output is not None else None
        self.store.db.execute("INSERT INTO tool_verifications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (f"tverify_{uuid.uuid4().hex}", share["tool_id"], share["version"], task_id, env_json, outcome, evidence_json, key, execution_id, input_hash, expected_json, now))

        row = self.store.db.execute("SELECT * FROM tool_verifications WHERE tool_id=? AND version=? AND independent_key=?", (share["tool_id"], share["version"], key)).fetchone()
        return {**dict(row), "actor_member_id": actor_member_id} if row else {"outcome": outcome, "independent_key": key, "actor_member_id": actor_member_id}



    def shares_for(self, member_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        self._member_dir(member_id)
        sql = "SELECT * FROM tool_shares WHERE recipient_member_id=?"
        args: list[Any] = [member_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        return [dict(row) for row in self.store.db.execute(sql + " ORDER BY created_at DESC", args)]

    def accept_share(self, recipient_member_id: str, share_id: str) -> dict[str, Any]:
        row = self.store.db.execute(
            "SELECT * FROM tool_shares WHERE share_id=? AND recipient_member_id=?",
            (share_id, recipient_member_id),
        ).fetchone()
        if not row:
            raise ValueError("tool_share_not_found")
        if row["status"] not in {"shared_candidate", "trial"}:
            raise ValueError("tool_share_not_acceptable")
        self.store.db.execute("UPDATE tool_shares SET status='trial' WHERE share_id=?", (share_id,))
        self._event(
            row["tool_id"], row["version"], "share_accepted",
            {"recipient_member_id": recipient_member_id, "share_id": share_id},
            f"tool:share:{share_id}:accept",
        )
        return {**dict(row), "status": "trial"}

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

        existing = self.store.db.execute("SELECT * FROM tool_shares WHERE tool_id=? AND version=? AND recipient_member_id=?", (tool_id, tool["current_version"], recipient_member_id)).fetchone()

        if existing:

            if existing["permission"] != permission:

                raise ValueError("tool_share_already_exists")

            return dict(existing)

        self.store.db.execute("INSERT INTO tool_shares (share_id, tool_id, version, owner_member_id, recipient_member_id, permission, status, created_at) VALUES (?,?,?,?,?,?,?,?)", (share_id, tool_id, tool["current_version"], owner_member_id, recipient_member_id, permission, "shared_candidate", time.time()))


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
