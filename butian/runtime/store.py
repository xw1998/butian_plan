from __future__ import annotations
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS members (member_id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, responsibilities TEXT NOT NULL, provider_id TEXT, model_id TEXT, default_strategy_id TEXT NOT NULL DEFAULT 'experience_reuse', strategy_version INTEGER NOT NULL DEFAULT 1, strategy_stages TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, mode TEXT NOT NULL, dispatcher_member_id TEXT, requested_model TEXT, effective_model TEXT, selection_status TEXT, capabilities TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, parent_id TEXT, session_id TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, assignee_id TEXT, revision INTEGER NOT NULL DEFAULT 1, lease_id TEXT, lease_until REAL, payload TEXT NOT NULL, strategy_snapshot TEXT NOT NULL DEFAULT '{}', UNIQUE(task_id, revision));
CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, aggregate_id TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, seq INTEGER NOT NULL, created_at REAL NOT NULL, commit_state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations (operation_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL, created_at REAL NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS handoffs (task_id TEXT NOT NULL, revision INTEGER NOT NULL, member_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(task_id, revision));
CREATE TABLE IF NOT EXISTS questions (question_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL, member_id TEXT NOT NULL, question TEXT NOT NULL, answer TEXT, status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS peer_learning (learning_id TEXT PRIMARY KEY, asker_id TEXT NOT NULL, expert_id TEXT NOT NULL, task_id TEXT, question TEXT NOT NULL, answer TEXT, comparison TEXT NOT NULL, status TEXT NOT NULL, candidate_memory_id TEXT, evidence_ids TEXT NOT NULL, created_at REAL NOT NULL, verified_at REAL);
CREATE INDEX IF NOT EXISTS idx_peer_learning_status ON peer_learning(status, expert_id);
CREATE TABLE IF NOT EXISTS evidence (evidence_id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, owner_member_id TEXT, expires_at REAL);
CREATE TABLE IF NOT EXISTS evidence_acl (evidence_id TEXT NOT NULL, member_id TEXT NOT NULL, granted_by TEXT NOT NULL, granted_at REAL NOT NULL, revoked_at REAL, PRIMARY KEY(evidence_id, member_id));
CREATE TABLE IF NOT EXISTS governance_audit (audit_id TEXT PRIMARY KEY, actor_member_id TEXT, action TEXT NOT NULL, target_id TEXT NOT NULL, details TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_verifications (verification_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, operation_key TEXT NOT NULL UNIQUE, outcome TEXT NOT NULL, evidence_ids TEXT NOT NULL, environment TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS member_memory_outbox (outbox_id TEXT PRIMARY KEY, operation_key TEXT NOT NULL UNIQUE, observation_id TEXT NOT NULL, member_id TEXT NOT NULL, memory_id TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, environment TEXT NOT NULL, source_event_ids TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS member_memory_outbox_events (event_id TEXT PRIMARY KEY, outbox_id TEXT NOT NULL, status TEXT NOT NULL, error TEXT, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_aggregate ON events(aggregate_id, seq);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE TABLE IF NOT EXISTS strategy_steps (step_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL, member_id TEXT, strategy_id TEXT NOT NULL, strategy_version INTEGER NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, output_ref TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS members_auth (member_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS provider_sessions (provider_session_id TEXT PRIMARY KEY, task_id TEXT, member_id TEXT NOT NULL, provider_id TEXT NOT NULL, model TEXT, status TEXT NOT NULL, last_provider_event_id TEXT, reason TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, capabilities TEXT NOT NULL DEFAULT '{}', requested_model TEXT, selection_status TEXT NOT NULL DEFAULT 'unknown', remote_session_id TEXT);
CREATE TABLE IF NOT EXISTS provider_outputs (provider_session_id TEXT NOT NULL, provider_event_id TEXT NOT NULL, text TEXT NOT NULL, model TEXT, created_at REAL NOT NULL, PRIMARY KEY(provider_session_id, provider_event_id));
CREATE INDEX IF NOT EXISTS idx_provider_sessions_task ON provider_sessions(task_id, status);
"""

def serialized_write(fn):
    def wrapped(self, *args, **kwargs):
        with self._write_lock:
            return fn(self, *args, **kwargs)
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._write_lock = threading.RLock()
        self.db.executescript(SCHEMA)
        self._migrate_columns()

    def _migrate_columns(self) -> None:
        member_columns = {row[1] for row in self.db.execute("PRAGMA table_info(members)")}
        if "persona" not in member_columns:
            self.db.execute("ALTER TABLE members ADD COLUMN persona TEXT")
        for column in ("rank", "reports_to", "reasoning_effort", "max_tokens", "model_tier", "tool_policy"):
            if column not in member_columns:
                self.db.execute(f"ALTER TABLE members ADD COLUMN {column} TEXT")
        if "default_strategy_id" not in member_columns:
            self.db.execute("ALTER TABLE members ADD COLUMN default_strategy_id TEXT NOT NULL DEFAULT 'experience_reuse'")
            self.db.execute("ALTER TABLE members ADD COLUMN strategy_version INTEGER NOT NULL DEFAULT 1")
            self.db.execute("ALTER TABLE members ADD COLUMN strategy_stages TEXT NOT NULL DEFAULT '[]'")
        task_columns = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
        if "strategy_snapshot" not in task_columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN strategy_snapshot TEXT NOT NULL DEFAULT '{}'")
        provider_columns = {row[1] for row in self.db.execute("PRAGMA table_info(provider_sessions)")}
        if "capabilities" not in provider_columns:
            self.db.execute("ALTER TABLE provider_sessions ADD COLUMN capabilities TEXT NOT NULL DEFAULT '{}'")
        if "requested_model" not in provider_columns:
            self.db.execute("ALTER TABLE provider_sessions ADD COLUMN requested_model TEXT")
        if "selection_status" not in provider_columns:
            self.db.execute("ALTER TABLE provider_sessions ADD COLUMN selection_status TEXT NOT NULL DEFAULT 'unknown'")
        if "remote_session_id" not in provider_columns:
            self.db.execute("ALTER TABLE provider_sessions ADD COLUMN remote_session_id TEXT")
        evidence_columns = {row[1] for row in self.db.execute("PRAGMA table_info(evidence)")}
        if "owner_member_id" not in evidence_columns:
            self.db.execute("ALTER TABLE evidence ADD COLUMN owner_member_id TEXT")
        if "expires_at" not in evidence_columns:
            self.db.execute("ALTER TABLE evidence ADD COLUMN expires_at REAL")
        outbox_columns = {row[1] for row in self.db.execute("PRAGMA table_info(member_memory_outbox)")}
        if "lease_until" not in outbox_columns:
            self.db.execute("ALTER TABLE member_memory_outbox ADD COLUMN lease_until REAL NOT NULL DEFAULT 0")
        provider_cols = {row[1] for row in self.db.execute("PRAGMA table_info(provider_sessions)")}
        if "prompt" not in provider_cols:
            self.db.execute("ALTER TABLE provider_sessions ADD COLUMN prompt TEXT")

        task_cols = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
        if "rework_count" not in task_cols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN rework_count INTEGER NOT NULL DEFAULT 0")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")

    def close(self) -> None:
        with self._write_lock:
            self.db.close()

    @serialized_write
    def create_provider_session(self, member_id: str, provider_id: str, *, task_id: str | None = None, model: str | None = None, provider_session_id: str | None = None, remote_session_id: str | None = None, capabilities: dict[str, Any] | None = None, requested_model: str | None = None, selection_status: str = "unknown", prompt: str | None = None) -> str:
        session_id = provider_session_id or f"psess_{uuid.uuid4().hex}"
        now = time.time()
        self.db.execute("INSERT INTO provider_sessions(provider_session_id,task_id,member_id,provider_id,model,status,last_provider_event_id,reason,created_at,updated_at,capabilities,requested_model,selection_status,remote_session_id,prompt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (session_id, task_id, member_id, provider_id, model, "running", None, None, now, now, json.dumps(capabilities or {}, ensure_ascii=False), requested_model, selection_status, remote_session_id, prompt))
        return session_id

    def provider_session(self, provider_session_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM provider_sessions WHERE provider_session_id=?", (provider_session_id,)).fetchone()
        if not row:
            raise ValueError("provider_session_not_found")
        return dict(row)

    def provider_session_access(self, provider_session_id: str, member_id: str) -> bool:
        row = self.db.execute("SELECT ps.member_id, s.dispatcher_member_id, t.assignee_id FROM provider_sessions ps LEFT JOIN tasks t ON t.task_id=ps.task_id LEFT JOIN sessions s ON s.session_id=t.session_id WHERE ps.provider_session_id=?", (provider_session_id,)).fetchone()
        return bool(row and member_id in {row["member_id"], row["dispatcher_member_id"], row["assignee_id"]})

    @serialized_write
    def record_provider_output(self, provider_session_id: str, provider_event_id: str, text: str, model: str | None = None) -> bool:
        with self.db:
            session = self.provider_session(provider_session_id)
            if session["status"] != "running":
                raise ValueError("provider_session_terminal")
            inserted = self.db.execute("INSERT OR IGNORE INTO provider_outputs VALUES (?,?,?,?,?)", (provider_session_id, provider_event_id, text, model, time.time())).rowcount
            if inserted:
                self.db.execute("UPDATE provider_sessions SET last_provider_event_id=?, updated_at=? WHERE provider_session_id=?", (provider_event_id, time.time(), provider_session_id))
        return bool(inserted)

    @serialized_write
    def finish_provider_session(self, provider_session_id: str, status: str, reason: str | None = None) -> dict[str, Any]:
        terminal = {"completed", "cancelled", "provider_lost", "unknown"}
        if status not in terminal | {"cancelling", "running"}:
            raise ValueError("invalid_provider_session_status")
        with self.db:
            current = self.provider_session(provider_session_id)["status"]
            allowed = {"running": terminal | {"cancelling"}, "cancelling": {"cancelled", "provider_lost", "unknown"}}
            if status not in allowed.get(current, set()):
                raise ValueError("invalid_provider_session_transition")
            self.db.execute("UPDATE provider_sessions SET status=?, reason=?, updated_at=? WHERE provider_session_id=?", (status, reason, time.time(), provider_session_id))
        return self.provider_session(provider_session_id)

    @serialized_write
    def add_member(self, member_id: str, name: str, role: str = "member", responsibilities: list[str] | None = None, provider_id: str | None = None, model_id: str | None = None, default_strategy_id: str = "experience_reuse", strategy_version: int = 1, strategy_stages: list[str] | None = None, *, persona: dict[str, Any] | None = None) -> None:
        from ..core.strategies import strategy_snapshot
        snapshot = strategy_snapshot(default_strategy_id, strategy_stages)
        identity = persona or {}
        columns = "member_id,name,role,responsibilities,provider_id,model_id,default_strategy_id,strategy_version,strategy_stages,persona,rank,reports_to,reasoning_effort,max_tokens,model_tier,tool_policy"
        values = (member_id, name, role, json.dumps(responsibilities or [], ensure_ascii=False), provider_id, model_id, default_strategy_id, strategy_version, json.dumps(snapshot["stages"], ensure_ascii=False), json.dumps(identity, ensure_ascii=False), identity.get("rank"), identity.get("reports_to"), identity.get("reasoning_effort"), identity.get("max_tokens"), identity.get("model_tier"), json.dumps(identity.get("tool_policy", {}), ensure_ascii=False))
        self.db.execute(f"INSERT INTO members({columns}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)

    @serialized_write
    def set_member_token(self, member_id: str, token: str) -> None:
        if not self.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
            raise ValueError("member_not_found")
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.db.execute("INSERT INTO members_auth VALUES (?, ?, ?) ON CONFLICT(member_id) DO UPDATE SET token_hash=excluded.token_hash", (member_id, token_hash, time.time()))

    def authenticate_member(self, member_id: str, token: str | None = None) -> bool:
        if not self.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
            return False
        row = self.db.execute("SELECT token_hash FROM members_auth WHERE member_id=?", (member_id,)).fetchone()
        return row is not None and token is not None and hmac.compare_digest(row["token_hash"], hashlib.sha256(token.encode("utf-8")).hexdigest())

    @serialized_write
    def task_access(self, task_id: str, member_id: str) -> bool:
        row = self.db.execute("SELECT t.assignee_id, s.dispatcher_member_id FROM tasks t JOIN sessions s ON s.session_id=t.session_id WHERE t.task_id=?", (task_id,)).fetchone()
        return bool(row and member_id in {row["assignee_id"], row["dispatcher_member_id"]})

    def claim_task(self, task_id: str, member_id: str, lease_seconds: int = 300, token: str | None = None, *, internal: bool = False) -> dict[str, Any]:
        if not internal and (not self.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone() or (token is not None and not self.authenticate_member(member_id, token))):
            raise ValueError("member_authentication_failed")
        lease_id = f"lease_{uuid.uuid4().hex}"; now = time.time()
        with self.db:
            row = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row: raise ValueError("task_not_found")
            if row["lease_until"] and row["lease_until"] > now and row["lease_id"]: raise ValueError("task_lease_held")
            if row["lease_id"] and row["lease_until"] and row["lease_until"] <= now:
                self.db.execute("UPDATE tasks SET status='ready', assignee_id=NULL, lease_id=NULL, lease_until=NULL, revision=revision+1 WHERE task_id=? AND revision=?", (task_id, row["revision"]))
                row = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            transition = __import__("butian.core.state_machine", fromlist=["transition"]).transition
            transition(__import__("butian.core.models", fromlist=["TaskStatus"]).TaskStatus(row["status"]), __import__("butian.core.models", fromlist=["TaskStatus"]).TaskStatus.CLAIMED)
            updated = self.db.execute("UPDATE tasks SET status='claimed', assignee_id=?, lease_id=?, lease_until=?, revision=revision+1 WHERE task_id=? AND revision=?", (member_id, lease_id, now + lease_seconds, task_id, row["revision"])).rowcount
            if updated != 1:
                raise ValueError("revision_conflict")
        return {"task_id": task_id, "lease_id": lease_id, "revision": row["revision"] + 1, "lease_until": now + lease_seconds}

    @serialized_write
    def reclaim_expired_leases(self, *, now: float | None = None) -> list[str]:
        now = time.time() if now is None else float(now)
        reclaimed: list[str] = []
        with self.db:
            rows = self.db.execute("SELECT task_id, revision FROM tasks WHERE lease_id IS NOT NULL AND lease_until IS NOT NULL AND lease_until<=? AND status IN ('claimed','running','cancelling')", (now,)).fetchall()
            for row in rows:
                updated = self.db.execute("UPDATE tasks SET status='ready', assignee_id=NULL, lease_id=NULL, lease_until=NULL, revision=revision+1 WHERE task_id=? AND revision=?", (row["task_id"], row["revision"])).rowcount
                if updated:
                    reclaimed.append(row["task_id"])
        return reclaimed

    @serialized_write
    def cancel_task(self, task_id: str, expected_revision: int, operation_key: str) -> None:
        row = self.db.execute("SELECT status FROM tasks WHERE task_id=? AND revision=?", (task_id, expected_revision)).fetchone()
        if not row:
            raise ValueError("revision_conflict")
        target = "cancelling" if row["status"] == "running" else "cancelled"
        self.transition_task(task_id, expected_revision, target, operation_key)

    @serialized_write
    def heartbeat(self, task_id: str, lease_id: str, lease_seconds: int = 300) -> None:
        updated = self.db.execute("UPDATE tasks SET lease_until=? WHERE task_id=? AND lease_id=? AND lease_until>?", (time.time() + lease_seconds, task_id, lease_id, time.time())).rowcount
        if updated != 1: raise ValueError("lease_invalid")

    @serialized_write
    def release_lease(self, task_id: str, lease_id: str) -> None:
        with self.db:
            row = self.db.execute("SELECT status FROM tasks WHERE task_id=? AND lease_id=?", (task_id, lease_id)).fetchone()
            if not row:
                raise ValueError("lease_invalid")
            if row["status"] == "claimed":
                self.db.execute("UPDATE tasks SET status='ready', assignee_id=NULL, lease_id=NULL, lease_until=NULL, revision=revision+1 WHERE task_id=? AND lease_id=?", (task_id, lease_id))
            else:
                self.db.execute("UPDATE tasks SET lease_id=NULL, lease_until=NULL WHERE task_id=? AND lease_id=?", (task_id, lease_id))

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    @serialized_write
    def set_setting(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def session(self, session_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if not row:
            raise ValueError("session_not_found")
        return dict(row)

    @serialized_write
    def switch_session_mode(self, session_id: str, mode: str) -> dict[str, Any]:
        """只改后续编排方式，不动任务与记忆，不篡改历史。"""
        if mode not in {"solo", "team"}:
            raise ValueError("invalid_session_mode")
        current = self.session(session_id)
        if current["mode"] == mode:
            return {"session_id": session_id, "from": mode, "to": mode, "changed": False}
        with self.db:
            self.db.execute("UPDATE sessions SET mode=? WHERE session_id=?", (mode, session_id))
        return {"session_id": session_id, "from": current["mode"], "to": mode, "changed": True}

    @serialized_write
    def create_session(self, mode: str, dispatcher_member_id: str | None = None, requested_model: str | None = None, capabilities: dict[str, Any] | None = None) -> str:
        if mode not in {"solo", "team"}:
            raise ValueError("invalid_session_mode")
        if dispatcher_member_id and not self.db.execute("SELECT 1 FROM members WHERE member_id=?", (dispatcher_member_id,)).fetchone():
            raise ValueError("dispatcher_member_not_found")
        session_id = f"sess_{uuid.uuid4().hex}"
        self.db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (session_id, mode, dispatcher_member_id, requested_model, None, "unknown", json.dumps(capabilities or {}), time.time()))
        return session_id

    @serialized_write
    def create_task(self, session_id: str, title: str, assignee_id: str | None = None, parent_id: str | None = None, payload: dict[str, Any] | None = None) -> str:
        self.session(session_id)
        if parent_id:
            parent = self.db.execute("SELECT status FROM tasks WHERE task_id=?", (parent_id,)).fetchone()
            if not parent:
                raise ValueError("parent_task_not_found")
            if parent["status"] in {"done", "cancelled"}:
                raise ValueError("parent_task_closed")
        task_id = f"task_{uuid.uuid4().hex}"
        self.db.execute("INSERT INTO tasks(task_id,parent_id,session_id,title,status,assignee_id,revision,payload,strategy_snapshot) VALUES (?,?,?,?,?,?,?,?,?)", (task_id, parent_id, session_id, title, "todo", assignee_id, 1, json.dumps(payload or {}, ensure_ascii=False), json.dumps({}, ensure_ascii=False)))
        return task_id

    @serialized_write
    def set_member_strategy(self, member_id: str, strategy_id: str, version: int = 1, stages: list[str] | None = None) -> dict[str, Any]:
        from ..core.strategies import strategy_snapshot
        snapshot = strategy_snapshot(strategy_id, stages)
        updated = self.db.execute("UPDATE members SET default_strategy_id=?, strategy_version=?, strategy_stages=? WHERE member_id=?", (strategy_id, version, json.dumps(snapshot["stages"], ensure_ascii=False), member_id)).rowcount
        if updated != 1:
            raise ValueError("member_not_found")
        return snapshot

    def strategy_for_member(self, member_id: str) -> dict[str, Any]:
        from ..core.strategies import strategy_snapshot
        row = self.db.execute("SELECT default_strategy_id, strategy_version, strategy_stages FROM members WHERE member_id=?", (member_id,)).fetchone()
        if not row:
            raise ValueError("member_not_found")
        snapshot = strategy_snapshot(row["default_strategy_id"], json.loads(row["strategy_stages"]) or None)
        snapshot["version"] = row["strategy_version"]
        return snapshot

    @serialized_write
    def assign_strategy(self, task_id: str, strategy_id: str, *, stages: list[str] | None = None, reason: str | None = None, decision_by: str | None = None) -> dict[str, Any]:
        from ..core.strategies import strategy_snapshot
        snapshot = strategy_snapshot(strategy_id, stages)
        snapshot.update({"override": bool(reason), "override_reason": reason, "decision_by": decision_by})
        updated = self.db.execute("UPDATE tasks SET strategy_snapshot=? WHERE task_id=?", (json.dumps(snapshot, ensure_ascii=False), task_id)).rowcount
        if updated != 1:
            raise ValueError("task_not_found")
        return snapshot

    @serialized_write
    def record_strategy_step(self, task_id: str, revision: int, strategy_id: str, stage: str, *, member_id: str | None = None, strategy_version: int = 1, status: str = "completed", reason: str | None = None, output_ref: str | None = None) -> str:
        from ..core.strategies import get_strategy
        strategy = get_strategy(strategy_id, strategy_version)
        if stage not in strategy.stages:
            raise ValueError("unknown_strategy_stage")
        if status not in {"planned", "running", "completed", "skipped", "failed", "unknown"}:
            raise ValueError("invalid_strategy_step_status")
        step_id = f"step_{uuid.uuid4().hex}"
        self.db.execute("INSERT INTO strategy_steps VALUES (?,?,?,?,?,?,?,?,?,?,?)", (step_id, task_id, revision, member_id, strategy_id, strategy_version, stage, status, reason, output_ref, time.time()))
        return step_id

    def list_strategy_steps(self, task_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM strategy_steps WHERE task_id=? ORDER BY created_at", (task_id,))]

    @serialized_write
    def transition_task(self, task_id: str, expected_revision: int, target: str, operation_key: str, payload: dict[str, Any] | None = None) -> None:
        existing = self.db.execute("SELECT state FROM operations WHERE idempotency_key=?", (operation_key,)).fetchone()
        if existing:
            if existing["state"] == "committed":
                return
            raise ValueError("operation_incomplete_requires_recovery")
        from ..core.models import TaskStatus
        from ..core.state_machine import transition
        row = self.db.execute("SELECT status, revision, payload FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row:
            transition(TaskStatus(row["status"]), TaskStatus(target))
        if not row or row["revision"] != expected_revision:
            raise ValueError("revision_conflict")
        if target == "done":
            self._require_valid_handoff_for_done(task_id, expected_revision)
            self._require_children_done(task_id)
        operation_id = f"op_{uuid.uuid4().hex}"
        now = time.time()
        with self._write_lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT INTO operations VALUES (?,?,?,?,?)", (operation_id, operation_key, "prepared", now, None))
            current_payload = json.loads(row["payload"] or "{}")
            next_payload = current_payload if payload is None else {**current_payload, **payload}
            updated = self.db.execute("UPDATE tasks SET status=?, revision=revision+1, payload=? WHERE task_id=? AND revision=?", (target, json.dumps(next_payload, ensure_ascii=False), task_id, expected_revision)).rowcount
            if updated != 1:
                raise ValueError("revision_conflict")
            seq = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            event_id = f"evt_{uuid.uuid4().hex}"
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", (event_id, operation_id, operation_key, task_id, "task_transition", json.dumps({"from": row["status"], "to": target, "revision": expected_revision + 1}, ensure_ascii=False), seq, now, "prepared"))
            self.db.execute("UPDATE operations SET state='committed' WHERE operation_id=?", (operation_id,))
            self.db.execute("UPDATE events SET commit_state='committed' WHERE operation_id=?", (operation_id,))
            self.db.execute("COMMIT")

    def _require_children_done(self, task_id: str) -> None:
        """父任务进入 done 前，所有未取消的子任务必须已完成。"""
        children = self.db.execute("SELECT task_id, status FROM tasks WHERE parent_id=?", (task_id,)).fetchall()
        for child in children:
            if child["status"] not in {"done", "cancelled"}:
                raise ValueError(f"children_not_done:{child['task_id']}")

    def _require_valid_handoff_for_done(self, task_id: str, revision: int) -> None:
        """review→done 审核门槛：必须有当前 revision 的交接包，且成员存在、证据非空且可查。"""
        handoff = self.db.execute(
            "SELECT payload FROM handoffs WHERE task_id=? AND revision=? AND status IN ('done','completed')",
            (task_id, revision),
        ).fetchone()
        if not handoff:
            raise ValueError("handoff_required_for_done")
        payload = json.loads(handoff["payload"])
        member_id = payload.get("member_id")
        if not member_id or not self.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
            raise ValueError("handoff_member_not_found")
        evidence_ids = payload.get("evidence_ids") or []
        if not evidence_ids:
            raise ValueError("handoff_evidence_required_for_done")
        for evidence_id in evidence_ids:
            if not self.db.execute("SELECT 1 FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone():
                raise ValueError(f"handoff_evidence_not_found:{evidence_id}")
            evidence = self.verify_evidence(evidence_id, actor_member_id=member_id)
            if evidence.get("status") not in {"verified", "valid"}:
                raise ValueError(f"handoff_evidence_invalid:{evidence_id}:{evidence.get('status')}")

    def validate_source_events(self, event_ids: list[str], *, member_id: str | None = None) -> bool:
        if not event_ids:
            return False
        placeholders = ",".join("?" for _ in event_ids)
        rows = list(self.db.execute(f"SELECT event_id, aggregate_id FROM events WHERE event_id IN ({placeholders})", event_ids))
        if len(rows) != len(set(event_ids)):
            return False
        if member_id:
            for row in rows:
                if row["aggregate_id"] == member_id:
                    continue
                linked = self.db.execute("SELECT 1 FROM peer_learning WHERE learning_id=? AND (asker_id=? OR expert_id=?)", (row["aggregate_id"], member_id, member_id)).fetchone()
                if not linked:
                    return False
        return True

    def task_tree(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """返回任务树：根任务（无 parent）嵌套 children，按创建顺序。"""
        if session_id:
            rows = [dict(row) for row in self.db.execute("SELECT * FROM tasks WHERE session_id=? ORDER BY rowid", (session_id,))]
        else:
            rows = [dict(row) for row in self.db.execute("SELECT * FROM tasks ORDER BY rowid")]
        by_id = {row["task_id"]: {**row, "children": []} for row in rows}
        roots: list[dict[str, Any]] = []
        for row in rows:
            node = by_id[row["task_id"]]
            parent = by_id.get(row["parent_id"]) if row["parent_id"] else None
            if parent is not None and parent is not node:
                parent["children"].append(node)
            else:
                roots.append(node)
        return roots

    @serialized_write
    def record_rework(self, task_id: str, revision: int, *, reviewer_member_id: str, notes: str, rework_limit: int) -> int:
        """review→ready 返工记账：超上限拒绝，不静默放行。"""
        row = self.db.execute("SELECT rework_count, status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("task_not_found")
        if row["status"] != "review":
            raise ValueError("task_not_in_review")
        new_count = int(row["rework_count"] or 0) + 1
        if new_count > rework_limit:
            raise ValueError("rework_limit_exceeded")
        with self.db:
            self.db.execute("UPDATE tasks SET rework_count=? WHERE task_id=?", (new_count, task_id))
        return new_count

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return [dict(row) for row in self.db.execute("SELECT * FROM tasks WHERE status=? ORDER BY rowid", (status,))]
        return [dict(row) for row in self.db.execute("SELECT * FROM tasks ORDER BY rowid")]

    @serialized_write
    def recover(self) -> list[dict[str, Any]]:
        """Recover prepared transitions using their durable event intent.

        A prepared operation is committed only when the task already reflects the
        event or can be advanced from the exact recorded predecessor state. Any
        mismatch remains incomplete for manual review; recovery never guesses.
        """
        pending = [dict(row) for row in self.db.execute("SELECT * FROM operations WHERE state IN ('prepared','pending_recovery')")]
        results = []
        for operation in pending:
            event = self.db.execute("SELECT * FROM events WHERE operation_id=? AND event_type='task_transition'", (operation["operation_id"],)).fetchone()
            if not event:
                with self.db:
                    self.db.execute("UPDATE operations SET state='incomplete', error=? WHERE operation_id=?", ("missing_recovery_intent", operation["operation_id"]))
                results.append({**operation, "recovery": "incomplete", "reason": "missing_recovery_intent"})
                continue
            intent = json.loads(event["payload"])
            task = self.db.execute("SELECT status, revision FROM tasks WHERE task_id=?", (event["aggregate_id"],)).fetchone()
            target_revision = int(intent["revision"])
            recovered = None
            with self.db:
                if task and task["status"] == intent["to"] and task["revision"] == target_revision:
                    recovered = "already_applied"
                elif task and task["status"] == intent["from"] and task["revision"] == target_revision - 1:
                    self.db.execute("UPDATE tasks SET status=?, revision=? WHERE task_id=? AND status=? AND revision=?", (intent["to"], target_revision, event["aggregate_id"], intent["from"], target_revision - 1))
                    recovered = "replayed"
                if recovered:
                    self.db.execute("UPDATE operations SET state='committed', error=NULL WHERE operation_id=?", (operation["operation_id"],))
                    self.db.execute("UPDATE events SET commit_state='committed' WHERE operation_id=?", (operation["operation_id"],))
                else:
                    reason = "task_state_does_not_match_recovery_intent"
                    self.db.execute("UPDATE operations SET state='incomplete', error=? WHERE operation_id=?", (reason, operation["operation_id"]))
                    self.db.execute("UPDATE events SET commit_state='incomplete' WHERE operation_id=? AND commit_state!='committed'", (operation["operation_id"],))
                    recovered = "incomplete"
            results.append({**operation, "recovery": recovered})
        return results

    def _audit(self, actor_member_id: str | None, action: str, target_id: str, details: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO governance_audit VALUES (?,?,?,?,?,?)", (f"audit_{uuid.uuid4().hex}", actor_member_id, action, target_id, json.dumps(details, ensure_ascii=False), time.time()))

    def _require_evidence_access(self, row, actor_member_id: str | None) -> None:
        if row["owner_member_id"] is None or actor_member_id == row["owner_member_id"]:
            return
        acl = self.db.execute("SELECT 1 FROM evidence_acl WHERE evidence_id=? AND member_id=? AND revoked_at IS NULL", (row["evidence_id"], actor_member_id)).fetchone()
        if not acl:
            raise ValueError("evidence_access_denied")

    @serialized_write
    def verify_evidence(self, evidence_id: str, *, actor_member_id: str | None = None) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if not row:
            raise ValueError("evidence_not_found")
        self._require_evidence_access(row, actor_member_id)
        if row["expires_at"] is not None and row["expires_at"] <= time.time():
            self.db.execute("UPDATE evidence SET status='expired' WHERE evidence_id=?", (evidence_id,))
            self._audit(actor_member_id, "evidence_expired_access", evidence_id, {})
            return {"evidence_id": evidence_id, "status": "expired", "sha256": None, "size": None}
        try:
            data = Path(row["path"]).read_bytes()
            current_hash = hashlib.sha256(data).hexdigest()
            current_size = len(data)
        except OSError:
            current_hash, current_size = None, None
        status = "valid" if current_hash == row["sha256"] and current_size == row["size"] else "stale"
        self.db.execute("UPDATE evidence SET status=? WHERE evidence_id=?", (status, evidence_id))
        self._audit(actor_member_id, "evidence_verified", evidence_id, {"status": status})
        return {"evidence_id": evidence_id, "status": status, "sha256": current_hash, "size": current_size}

    @serialized_write
    def grant_evidence_access(self, evidence_id: str, member_id: str, *, actor_member_id: str, token: str | None = None, internal: bool = False) -> None:
        row = self.db.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if not row:
            raise ValueError("evidence_not_found")
        if not internal and (not self.db.execute("SELECT 1 FROM members WHERE member_id=?", (actor_member_id,)).fetchone() or (token is not None and not self.authenticate_member(actor_member_id, token))):
            raise ValueError("member_authentication_failed")
        if row["owner_member_id"] != actor_member_id:
            raise ValueError("evidence_owner_required")
        if not self.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
            raise ValueError("member_not_found")
        with self.db:
            self.db.execute("INSERT INTO evidence_acl VALUES (?,?,?,?,NULL) ON CONFLICT(evidence_id,member_id) DO UPDATE SET granted_by=excluded.granted_by,granted_at=excluded.granted_at,revoked_at=NULL", (evidence_id, member_id, actor_member_id, time.time()))
            self._audit(actor_member_id, "evidence_access_granted", evidence_id, {"member_id": member_id})

    @serialized_write
    def revoke_evidence_access(self, evidence_id: str, member_id: str, *, actor_member_id: str, token: str | None = None) -> None:
        row = self.db.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if not row:
            raise ValueError("evidence_not_found")
        if not self.authenticate_member(actor_member_id, token):
            raise ValueError("member_authentication_failed")
        if row["owner_member_id"] != actor_member_id:
            raise ValueError("evidence_owner_required")
        with self.db:
            self.db.execute("UPDATE evidence_acl SET revoked_at=? WHERE evidence_id=? AND member_id=? AND revoked_at IS NULL", (time.time(), evidence_id, member_id))
            self._audit(actor_member_id, "evidence_access_revoked", evidence_id, {"member_id": member_id})

    @serialized_write
    def authorize_memory_action(self, member_id: str, action: str, *, token: str | None, authorized: bool, reason: str) -> str:
        if action not in {"export", "delete"}:
            raise ValueError("invalid_memory_governance_action")
        if not self.authenticate_member(member_id, token):
            raise ValueError("member_authentication_failed")
        if not authorized or not reason.strip():
            raise ValueError("explicit_authorization_and_reason_required")
        authorization_id = f"auth_{uuid.uuid4().hex}"
        with self.db:
            self.db.execute("INSERT INTO meta(key,value) VALUES (?,?)", (authorization_id, json.dumps({"member_id": member_id, "action": action, "reason": reason, "created_at": time.time()})))
            self._audit(member_id, f"memory_{action}_authorized", member_id, {"authorization_id": authorization_id, "reason": reason})
        return authorization_id

    @serialized_write
    def append_event(self, aggregate_id: str, event_type: str, payload: dict[str, Any], operation_key: str) -> str:
        existing = self.db.execute("SELECT event_id FROM events WHERE idempotency_key=?", (operation_key,)).fetchone()
        if existing:
            return existing[0]
        event_id = f"evt_{uuid.uuid4().hex}"
        operation_id = f"op_{uuid.uuid4().hex}"
        seq = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
        self.db.execute("INSERT INTO operations VALUES (?,?,?,?,?)", (operation_id, operation_key, "committed", time.time(), None))
        self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", (event_id, operation_id, operation_key, aggregate_id, event_type, json.dumps(payload, ensure_ascii=False), seq, time.time(), "committed"))
        return event_id

    @serialized_write
    def enqueue_member_memory(self, observation_id: str, member_id: str, memory_id: str, kind: str, content: str, environment: dict[str, Any], source_event_ids: list[str]) -> str:
        operation_key = f"member-memory:{observation_id}:{member_id}"
        outbox_id = f"outbox_{uuid.uuid4().hex}"
        now = time.time()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO member_memory_outbox(outbox_id,operation_key,observation_id,member_id,memory_id,kind,content,environment,source_event_ids,status,attempts,last_error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'pending',0,NULL,?,?)", (outbox_id, operation_key, observation_id, member_id, memory_id, kind, content, json.dumps(environment or {}, ensure_ascii=False), json.dumps(source_event_ids, ensure_ascii=False), now, now))
            row = self.db.execute("SELECT outbox_id FROM member_memory_outbox WHERE operation_key=?", (operation_key,)).fetchone()
            self.db.execute("INSERT INTO member_memory_outbox_events VALUES (?,?,?,?,?)", (f"mmev_{uuid.uuid4().hex}", row[0], "pending", None, now))
        return row[0]

    @serialized_write
    def claim_member_memory_outbox(self, *, limit: int = 20, lease_seconds: int = 60) -> list[dict[str, Any]]:
        if limit <= 0 or lease_seconds <= 0:
            raise ValueError("invalid_outbox_claim_limits")
        now = time.time()
        claimed = []
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = self.db.execute("SELECT * FROM member_memory_outbox WHERE status='pending' OR (status='processing' AND lease_until<?) ORDER BY created_at LIMIT ?", (now, limit)).fetchall()
            for row in rows:
                outbox_id = row["outbox_id"]
                updated = self.db.execute("UPDATE member_memory_outbox SET status='processing',attempts=attempts+1,lease_until=?,updated_at=? WHERE outbox_id=? AND (status='pending' OR (status='processing' AND lease_until<?))", (now + lease_seconds, now, outbox_id, now)).rowcount
                if updated:
                    self.db.execute("INSERT INTO member_memory_outbox_events VALUES (?,?,?,?,?)", (f"mmev_{uuid.uuid4().hex}", outbox_id, "processing", None, now))
                    claimed.append(dict(self.db.execute("SELECT * FROM member_memory_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return claimed

    def list_member_memory_outbox(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status is None:
            rows = self.db.execute("SELECT * FROM member_memory_outbox ORDER BY created_at")
        else:
            rows = self.db.execute("SELECT * FROM member_memory_outbox WHERE status=? ORDER BY created_at", (status,))
        return [dict(row) for row in rows]

    @serialized_write
    def mark_member_memory_outbox(self, outbox_id: str, status: str, *, error: str | None = None) -> None:
        if status not in {"pending", "processing", "committed", "failed"}:
            raise ValueError("invalid_outbox_status")
        now = time.time()
        with self.db:
            updated = self.db.execute("UPDATE member_memory_outbox SET status=?, lease_until=0, last_error=?, updated_at=? WHERE outbox_id=?", (status, error, now, outbox_id)).rowcount
            if updated != 1:
                raise ValueError("member_memory_outbox_not_found")
            self.db.execute("INSERT INTO member_memory_outbox_events VALUES (?,?,?,?,?)", (f"mmev_{uuid.uuid4().hex}", outbox_id, status, error, now))

    @serialized_write
    def record_verification(self, memory_id: str, operation_key: str, outcome: str, evidence_ids: list[str], environment: dict[str, Any] | None = None, *, actor_member_id: str | None = None):
        if outcome not in {"success", "failure", "unknown"}:
            raise ValueError("invalid_verification_outcome")
        if outcome == "success" and not evidence_ids:
            raise ValueError("evidence_required_for_success")
        if self.db.execute("SELECT 1 FROM memory_verifications WHERE operation_key=?", (operation_key,)).fetchone():
            return self.db.execute("SELECT verification_id FROM memory_verifications WHERE operation_key=?", (operation_key,)).fetchone()[0]
        for evidence_id in evidence_ids:
            result = self.verify_evidence(evidence_id, actor_member_id=actor_member_id)
            if result["status"] != "valid":
                raise ValueError("stale_evidence")
        verification_id = f"ver_{uuid.uuid4().hex}"
        self.db.execute("INSERT INTO memory_verifications VALUES (?,?,?,?,?,?,?)", (verification_id, memory_id, operation_key, outcome, json.dumps(evidence_ids), json.dumps(environment or {}, ensure_ascii=False), time.time()))
        return verification_id

    @serialized_write
    def record_evidence(self, path: str | Path, root: str | Path | None = None, *, owner_member_id: str | None = None, expires_at: float | None = None, actor_member_id: str | None = None, token: str | None = None, internal: bool = False) -> str:
        p = Path(path).resolve()
        if root is not None:
            base = Path(root).resolve()
            if base not in p.parents and p != base: raise ValueError("evidence_path_outside_root")
        if expires_at is not None and expires_at <= time.time():
            raise ValueError("evidence_expiry_must_be_future")
        if owner_member_id is not None and (actor_member_id != owner_member_id or (not internal and (not self.db.execute("SELECT 1 FROM members WHERE member_id=?", (actor_member_id,)).fetchone() or (token is not None and not self.authenticate_member(actor_member_id, token))))):
            raise ValueError("member_authentication_failed")
        if owner_member_id is None and actor_member_id is not None:
            raise ValueError("evidence_owner_required")
        data = p.read_bytes()
        evidence_id = f"ev_{uuid.uuid4().hex}"
        with self.db:
            self.db.execute("INSERT INTO evidence(evidence_id,path,sha256,size,status,created_at,owner_member_id,expires_at) VALUES (?,?,?,?,?,?,?,?)", (evidence_id, str(p), hashlib.sha256(data).hexdigest(), len(data), "valid", time.time(), owner_member_id, expires_at))
            self._audit(actor_member_id, "evidence_recorded", evidence_id, {"owner_member_id": owner_member_id, "expires_at": expires_at})
        return evidence_id
