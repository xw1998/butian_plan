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

    def ask_peer(self, asker_id: str, expert_id: str, question: str, *, task_id: str | None = None, comparison: dict[str, Any] | None = None):
        if asker_id == expert_id:
            raise ValueError("peer_learning_requires_two_members")
        for member_id in (asker_id, expert_id):
            if not self.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
                raise ValueError("member_not_found:" + member_id)
        learning_id = f"learn_{uuid.uuid4().hex}"
        event_id = self.store.append_event(learning_id, "peer_learning_asked", {"asker_id": asker_id, "expert_id": expert_id, "task_id": task_id, "question": question}, f"{learning_id}:asked")
        self.store.db.execute("INSERT INTO peer_learning VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (learning_id, asker_id, expert_id, task_id, question, None, json.dumps(comparison or {}, ensure_ascii=False), "asked", None, json.dumps([event_id]), time.time(), None))
        return learning_id

    def answer_peer(self, learning_id: str, answer: str, *, method: str | None = None, evidence_ids: list[str] | None = None):
        row = self.store.db.execute("SELECT * FROM peer_learning WHERE learning_id=?", (learning_id,)).fetchone()
        if not row or row["status"] != "asked":
            raise ValueError("peer_learning_not_open")
        candidate_id = f"mem_{uuid.uuid4().hex}"
        candidate = {"answer": answer, "method": method, "expert_id": row["expert_id"], "learning_id": learning_id, "verification_required": True}
        memory = self.memory(row["asker_id"])
        try:
            answer_event = self.store.append_event(learning_id, "peer_learning_answered", {"answer": answer, "method": method, "expert_id": row["expert_id"]}, f"{learning_id}:answered")
            memory.put(candidate_id, "peer_learning_candidate", json.dumps(candidate, ensure_ascii=False), confidence=0.2, weight=0.2, environment={"source": "peer", "expert_id": row["expert_id"]}, source_event_ids=[answer_event], status="candidate")
        finally:
            memory.close()
        ids = evidence_ids or []
        self.store.db.execute("UPDATE peer_learning SET answer=?, status='candidate', candidate_memory_id=?, evidence_ids=? WHERE learning_id=?", (answer, candidate_id, json.dumps(ids), learning_id))
        return {"learning_id": learning_id, "candidate_memory_id": candidate_id, "status": "candidate"}

    def verify_peer_learning(self, learning_id: str, *, success: bool, evidence_ids: list[str] | None = None, notes: str = ""):
        row = self.store.db.execute("SELECT * FROM peer_learning WHERE learning_id=?", (learning_id,)).fetchone()
        if not row or row["status"] not in ("candidate", "verified", "rejected", "inconclusive"):
            raise ValueError("peer_learning_not_candidate")
        status = "verified" if success else "rejected"
        if success:
            self.store.record_verification(row["candidate_memory_id"], f"{learning_id}:verification:{time.time_ns()}", "success", evidence_ids or [], {"peer_learning_id": learning_id})
        else:
            self.store.record_verification(row["candidate_memory_id"], f"{learning_id}:verification:{time.time_ns()}", "failure", evidence_ids or [], {"peer_learning_id": learning_id})
        memory = self.memory(row["asker_id"])
        try:
            mem = memory.db.execute("SELECT * FROM memories WHERE memory_id=?", (row["candidate_memory_id"],)).fetchone()
            if mem:
                successful = memory.db.execute("SELECT COUNT(*) FROM memory_events WHERE memory_id=? AND event_type='verification_success'", (row["candidate_memory_id"],)).fetchone()[0]
                if success:
                    memory.db.execute("INSERT INTO memory_events VALUES (?,?,?,?,?)", (f"mev_{uuid.uuid4().hex}", row["candidate_memory_id"], "verification_success", json.dumps({"learning_id": learning_id, "evidence_ids": evidence_ids or []}), time.time()))
                    successful += 1
                else:
                    memory.db.execute("INSERT INTO memory_events VALUES (?,?,?,?,?)", (f"mev_{uuid.uuid4().hex}", row["candidate_memory_id"], "verification_failure", json.dumps({"learning_id": learning_id, "evidence_ids": evidence_ids or []}), time.time()))
                new_status = "warm" if successful >= 2 else "candidate"
                new_weight = min(5.0, float(mem["weight"]) + (0.5 if success else -0.2))
                new_confidence = min(1.0, float(mem["confidence"]) + (0.1 if success else 0.0))
                memory.db.execute("UPDATE memories SET status=?, weight=?, confidence=?, updated_at=? WHERE memory_id=?", (new_status, new_weight, new_confidence, time.time(), row["candidate_memory_id"]))
        finally:
            memory.close()
        ids = evidence_ids or json.loads(row["evidence_ids"])
        with self.store.db:
            self.store.db.execute("UPDATE peer_learning SET status=?, evidence_ids=?, verified_at=?, comparison=? WHERE learning_id=?", (status, json.dumps(ids), time.time(), json.dumps({**json.loads(row["comparison"]), "verification_notes": notes, "success": success}, ensure_ascii=False), learning_id))
        return {"learning_id": learning_id, "status": status, "candidate_memory_id": row["candidate_memory_id"], "memory_status": new_status if 'new_status' in locals() else "candidate"}

    def list_peer_learning(self, *, status: str | None = None, member_id: str | None = None):
        sql = "SELECT * FROM peer_learning WHERE 1=1"; args: list[Any] = []
        if status: sql += " AND status=?"; args.append(status)
        if member_id: sql += " AND (asker_id=? OR expert_id=?)"; args.extend([member_id, member_id])
        return [dict(row) for row in self.store.db.execute(sql + " ORDER BY created_at DESC", args)]

    def submit_handoff(self, handoff: dict[str, Any]):
        required = {"task_id", "task_revision", "member_id", "status", "summary", "evidence_ids", "next_action", "confidence"}
        missing = required - handoff.keys()
        if missing:
            raise ValueError(f"handoff_missing:{','.join(sorted(missing))}")
        self.store.db.execute("INSERT OR REPLACE INTO handoffs VALUES (?,?,?,?,?,?)", (handoff["task_id"], handoff["task_revision"], handoff["member_id"], handoff["status"], json.dumps(handoff, ensure_ascii=False), time.time()))
        return True
