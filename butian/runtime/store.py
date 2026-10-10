from __future__ import annotations
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS members (member_id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, responsibilities TEXT NOT NULL, provider_id TEXT, model_id TEXT);
CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, mode TEXT NOT NULL, dispatcher_member_id TEXT, requested_model TEXT, effective_model TEXT, selection_status TEXT, capabilities TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, parent_id TEXT, session_id TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, assignee_id TEXT, revision INTEGER NOT NULL DEFAULT 1, lease_id TEXT, lease_until REAL, payload TEXT NOT NULL, UNIQUE(task_id, revision));
CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, aggregate_id TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, seq INTEGER NOT NULL, created_at REAL NOT NULL, commit_state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations (operation_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL, created_at REAL NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS handoffs (task_id TEXT NOT NULL, revision INTEGER NOT NULL, member_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(task_id, revision));
CREATE TABLE IF NOT EXISTS questions (question_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL, member_id TEXT NOT NULL, question TEXT NOT NULL, answer TEXT, status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS peer_learning (learning_id TEXT PRIMARY KEY, asker_id TEXT NOT NULL, expert_id TEXT NOT NULL, task_id TEXT, question TEXT NOT NULL, answer TEXT, comparison TEXT NOT NULL, status TEXT NOT NULL, candidate_memory_id TEXT, evidence_ids TEXT NOT NULL, created_at REAL NOT NULL, verified_at REAL);
CREATE INDEX IF NOT EXISTS idx_peer_learning_status ON peer_learning(status, expert_id);
CREATE TABLE IF NOT EXISTS evidence (evidence_id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_verifications (verification_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, operation_key TEXT NOT NULL UNIQUE, outcome TEXT NOT NULL, evidence_ids TEXT NOT NULL, environment TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_aggregate ON events(aggregate_id, seq);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE TABLE IF NOT EXISTS members_auth (member_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, created_at REAL NOT NULL);
"""

class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add_member(self, member_id: str, name: str, role: str = "member", responsibilities: list[str] | None = None, provider_id: str | None = None, model_id: str | None = None) -> None:
        self.db.execute("INSERT INTO members VALUES (?, ?, ?, ?, ?, ?)", (member_id, name, role, json.dumps(responsibilities or [], ensure_ascii=False), provider_id, model_id))

    def set_member_token(self, member_id: str, token: str) -> None:
        if not self.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
            raise ValueError("member_not_found")
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.db.execute("INSERT INTO members_auth VALUES (?, ?, ?) ON CONFLICT(member_id) DO UPDATE SET token_hash=excluded.token_hash", (member_id, token_hash, time.time()))

    def authenticate_member(self, member_id: str, token: str | None = None) -> bool:
        if not self.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
            return False
        row = self.db.execute("SELECT token_hash FROM members_auth WHERE member_id=?", (member_id,)).fetchone()
        return row is None or (token is not None and hmac.compare_digest(row["token_hash"], hashlib.sha256(token.encode("utf-8")).hexdigest()))

    def claim_task(self, task_id: str, member_id: str, lease_seconds: int = 300, token: str | None = None) -> dict[str, Any]:
        if not self.authenticate_member(member_id, token):
            raise ValueError("member_authentication_failed")
        lease_id = f"lease_{uuid.uuid4().hex}"; now = time.time()
        with self.db:
            row = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row: raise ValueError("task_not_found")
            if row["lease_until"] and row["lease_until"] > now and row["lease_id"]: raise ValueError("task_lease_held")
            transition = __import__("butian.core.state_machine", fromlist=["transition"]).transition
            transition(__import__("butian.core.models", fromlist=["TaskStatus"]).TaskStatus(row["status"]), __import__("butian.core.models", fromlist=["TaskStatus"]).TaskStatus.CLAIMED)
            updated = self.db.execute("UPDATE tasks SET status='claimed', assignee_id=?, lease_id=?, lease_until=?, revision=revision+1 WHERE task_id=? AND revision=?", (member_id, lease_id, now + lease_seconds, task_id, row["revision"])).rowcount
            if updated != 1:
                raise ValueError("revision_conflict")
        return {"task_id": task_id, "lease_id": lease_id, "revision": row["revision"] + 1, "lease_until": now + lease_seconds}

    def heartbeat(self, task_id: str, lease_id: str, lease_seconds: int = 300) -> None:
        updated = self.db.execute("UPDATE tasks SET lease_until=? WHERE task_id=? AND lease_id=? AND lease_until>?", (time.time() + lease_seconds, task_id, lease_id, time.time())).rowcount
        if updated != 1: raise ValueError("lease_invalid")

    def release_lease(self, task_id: str, lease_id: str) -> None:
        with self.db:
            row = self.db.execute("SELECT status FROM tasks WHERE task_id=? AND lease_id=?", (task_id, lease_id)).fetchone()
            if not row:
                raise ValueError("lease_invalid")
            if row["status"] == "claimed":
                self.db.execute("UPDATE tasks SET status='ready', assignee_id=NULL, lease_id=NULL, lease_until=NULL, revision=revision+1 WHERE task_id=? AND lease_id=?", (task_id, lease_id))
            else:
                self.db.execute("UPDATE tasks SET lease_id=NULL, lease_until=NULL WHERE task_id=? AND lease_id=?", (task_id, lease_id))

    def create_session(self, mode: str, dispatcher_member_id: str | None, requested_model: str | None, capabilities: dict[str, Any]) -> str:
        if dispatcher_member_id and not self.db.execute("SELECT 1 FROM members WHERE member_id=?", (dispatcher_member_id,)).fetchone():
            raise ValueError("dispatcher_member_not_found")
        session_id = f"sess_{uuid.uuid4().hex}"
        self.db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (session_id, mode, dispatcher_member_id, requested_model, None, "unknown", json.dumps(capabilities), time.time()))
        return session_id

    def create_task(self, session_id: str, title: str, assignee_id: str | None = None, parent_id: str | None = None, payload: dict[str, Any] | None = None) -> str:
        task_id = f"task_{uuid.uuid4().hex}"
        self.db.execute("INSERT INTO tasks(task_id,parent_id,session_id,title,status,assignee_id,revision,payload) VALUES (?,?,?,?,?,?,?,?)", (task_id, parent_id, session_id, title, "todo", assignee_id, 1, json.dumps(payload or {}, ensure_ascii=False)))
        return task_id

    def transition_task(self, task_id: str, expected_revision: int, target: str, operation_key: str, payload: dict[str, Any] | None = None) -> None:
        existing = self.db.execute("SELECT state FROM operations WHERE idempotency_key=?", (operation_key,)).fetchone()
        if existing:
            return
        from ..core.models import TaskStatus
        from ..core.state_machine import transition
        row = self.db.execute("SELECT status, revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row:
            transition(TaskStatus(row["status"]), TaskStatus(target))
        if not row or row["revision"] != expected_revision:
            raise ValueError("revision_conflict")
        operation_id = f"op_{uuid.uuid4().hex}"
        now = time.time()
        with self.db:
            self.db.execute("INSERT INTO operations VALUES (?,?,?,?,?)", (operation_id, operation_key, "prepared", now, None))
            updated = self.db.execute("UPDATE tasks SET status=?, revision=revision+1, payload=? WHERE task_id=? AND revision=?", (target, json.dumps(payload or {}, ensure_ascii=False), task_id, expected_revision)).rowcount
            if updated != 1:
                raise ValueError("revision_conflict")
            seq = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            event_id = f"evt_{uuid.uuid4().hex}"
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", (event_id, operation_id, operation_key, task_id, "task_transition", json.dumps({"from": row["status"], "to": target, "revision": expected_revision + 1}, ensure_ascii=False), seq, now, "prepared"))
            self.db.execute("UPDATE operations SET state='committed' WHERE operation_id=?", (operation_id,))
            self.db.execute("UPDATE events SET commit_state='committed' WHERE operation_id=?", (operation_id,))

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

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return [dict(row) for row in self.db.execute("SELECT * FROM tasks WHERE status=? ORDER BY rowid", (status,))]
        return [dict(row) for row in self.db.execute("SELECT * FROM tasks ORDER BY rowid")]

    def recover(self) -> list[dict[str, Any]]:
        pending = [dict(row) for row in self.db.execute("SELECT * FROM operations WHERE state IN ('prepared','pending_recovery')")]
        for operation in pending:
            with self.db:
                self.db.execute("UPDATE operations SET state='incomplete', error=? WHERE operation_id=?", ("requires replay or manual review", operation["operation_id"]))
                self.db.execute("UPDATE events SET commit_state='incomplete' WHERE operation_id=? AND commit_state!='committed'", (operation["operation_id"],))
        return pending

    def verify_evidence(self, evidence_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if not row:
            raise ValueError("evidence_not_found")
        try:
            data = Path(row["path"]).read_bytes()
            current_hash = hashlib.sha256(data).hexdigest()
            current_size = len(data)
        except OSError:
            current_hash, current_size = None, None
        status = "valid" if current_hash == row["sha256"] and current_size == row["size"] else "stale"
        self.db.execute("UPDATE evidence SET status=? WHERE evidence_id=?", (status, evidence_id))
        return {"evidence_id": evidence_id, "status": status, "sha256": current_hash, "size": current_size}

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

    def record_verification(self, memory_id: str, operation_key: str, outcome: str, evidence_ids: list[str], environment: dict[str, Any] | None = None) -> str:
        if outcome not in {"success", "failure", "unknown"}:
            raise ValueError("invalid_verification_outcome")
        if outcome == "success" and not evidence_ids:
            raise ValueError("evidence_required_for_success")
        if self.db.execute("SELECT 1 FROM memory_verifications WHERE operation_key=?", (operation_key,)).fetchone():
            return self.db.execute("SELECT verification_id FROM memory_verifications WHERE operation_key=?", (operation_key,)).fetchone()[0]
        for evidence_id in evidence_ids:
            result = self.verify_evidence(evidence_id)
            if result["status"] != "valid":
                raise ValueError("stale_evidence")
        verification_id = f"ver_{uuid.uuid4().hex}"
        self.db.execute("INSERT INTO memory_verifications VALUES (?,?,?,?,?,?,?)", (verification_id, memory_id, operation_key, outcome, json.dumps(evidence_ids), json.dumps(environment or {}, ensure_ascii=False), time.time()))
        return verification_id

    def record_evidence(self, path: str | Path, root: str | Path | None = None) -> str:
        p = Path(path).resolve()
        if root is not None:
            base = Path(root).resolve()
            if base not in p.parents and p != base: raise ValueError("evidence_path_outside_root")
        data = p.read_bytes()
        evidence_id = f"ev_{uuid.uuid4().hex}"
        self.db.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?)", (evidence_id, str(p), hashlib.sha256(data).hexdigest(), len(data), "valid", time.time()))
        return evidence_id
