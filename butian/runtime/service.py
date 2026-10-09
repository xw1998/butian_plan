from __future__ import annotations
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from .memory import MemoryStore
from .store import Store

class ButianService:
    def __init__(self, home: str | Path | None = None):
        self.home = Path(home or os.environ.get("BUTIAN_HOME", Path.home() / ".butian"))
        self.home.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.home / "team.db")

    def close(self):
        self.store.close()

    def add_member(self, member_id: str, name: str, role="member", responsibilities=None, provider_id=None, model_id=None):
        self.store.add_member(member_id, name, role, responsibilities, provider_id, model_id)
        MemoryStore(self.home / "members" / member_id / "memory.db").close()

    def memory(self, member_id: str) -> MemoryStore:
        return MemoryStore(self.home / "members" / member_id / "memory.db")

    def create_session(self, mode: str, dispatcher_member_id: str | None = None, requested_model: str | None = None, capabilities=None):
        caps = capabilities or {}
        session_id = self.store.create_session(mode, dispatcher_member_id, requested_model, caps)
        return {"session_id": session_id, "mode": mode, "dispatcher_member_id": dispatcher_member_id, "requested_model": requested_model, "effective_model": None, "selection_status": "unknown", "capabilities": caps}

    def select_model(self, session_id: str, requested_model: str | None, capabilities: dict[str, Any], available_models: list[str] | None = None):
        status, effective, reason = "unknown", None, "provider did not report model selection"
        if requested_model and capabilities.get("can_select_model") and (not available_models or requested_model in available_models):
            status, effective, reason = "applied", requested_model, None
        elif requested_model and capabilities.get("can_select_model"):
            status, effective, reason = "fallback", (available_models or [None])[0], "requested model unavailable"
        elif requested_model:
            status, effective, reason = "unsupported", None, "provider cannot select a per-member model"
        self.store.db.execute("UPDATE sessions SET requested_model=?, effective_model=?, selection_status=?, capabilities=? WHERE session_id=?", (requested_model, effective, status, json.dumps({**capabilities, "model_selection_reason": reason}), session_id))
        return {"requested_model": requested_model, "effective_model": effective, "selection_status": status, "fallback_reason": reason}

    def ask_user(self, task_id: str, revision: int, member_id: str, question: str):
        qid = f"q_{uuid.uuid4().hex}"
        self.store.db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?,?,?)", (qid, task_id, revision, member_id, question, None, "open", time.time()))
        self.store.transition_task(task_id, revision, "waiting_user", f"question:{qid}", {"question_id": qid})
        return qid

    def answer_user(self, question_id: str, answer: str):
        row = self.store.db.execute("SELECT * FROM questions WHERE question_id=?", (question_id,)).fetchone()
        if not row or row["status"] != "open":
            raise ValueError("question_not_open")
        task = self.store.db.execute("SELECT revision FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        if not task or task["revision"] != row["revision"] + 1:
            raise ValueError("question_revision_conflict")
        with self.store.db:
            self.store.db.execute("UPDATE questions SET answer=?, status='answered' WHERE question_id=?", (answer, question_id))
            self.store.db.execute("UPDATE tasks SET status='ready', revision=revision+1, payload=? WHERE task_id=? AND revision=?", (json.dumps({"answer": answer}), row["task_id"], task["revision"]))
        return row["task_id"]

    def submit_handoff(self, handoff: dict[str, Any]):
        required = {"task_id", "task_revision", "member_id", "status", "summary", "evidence_ids", "next_action", "confidence"}
        missing = required - handoff.keys()
        if missing:
            raise ValueError(f"handoff_missing:{','.join(sorted(missing))}")
        self.store.db.execute("INSERT OR REPLACE INTO handoffs VALUES (?,?,?,?,?,?)", (handoff["task_id"], handoff["task_revision"], handoff["member_id"], handoff["status"], json.dumps(handoff, ensure_ascii=False), time.time()))
        return True
