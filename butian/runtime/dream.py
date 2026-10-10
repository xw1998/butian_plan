from __future__ import annotations
import json, time, uuid
from pathlib import Path
from .memory import MemoryStore

class DreamRunner:
    def __init__(self, memory: MemoryStore):
        self.memory = memory
        self.memory.db.executescript('''
        CREATE TABLE IF NOT EXISTS dream_runs (run_id TEXT PRIMARY KEY, started_at REAL NOT NULL, status TEXT NOT NULL, gate_snapshot TEXT NOT NULL, candidate_count INTEGER NOT NULL DEFAULT 0, reason TEXT);
        CREATE TABLE IF NOT EXISTS hypotheses (hypothesis_id TEXT PRIMARY KEY, candidate_id TEXT, statement TEXT NOT NULL, prior_reliability REAL NOT NULL, argument TEXT NOT NULL, status TEXT NOT NULL, evidence_refs TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS verification_actions (action_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, description TEXT NOT NULL, cost REAL NOT NULL, risk REAL NOT NULL, expected_information_gain REAL NOT NULL, authorized INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS verification_queue (queue_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, action_id TEXT NOT NULL, priority REAL NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS dream_candidates (candidate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, memory_ids TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS dream_lock (lock_id INTEGER PRIMARY KEY CHECK(lock_id=1), owner TEXT NOT NULL, acquired_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_distillations (distillation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, source_memory_ids TEXT NOT NULL, summary TEXT NOT NULL, merged_items TEXT NOT NULL, conflicts TEXT NOT NULL, discarded_items TEXT NOT NULL, information_loss REAL NOT NULL, reversible INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_abstractions (abstraction_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, source_memory_ids TEXT NOT NULL, pattern TEXT NOT NULL, supports TEXT NOT NULL, counterexamples TEXT NOT NULL, applicability TEXT NOT NULL, failure_conditions TEXT NOT NULL, abstraction_level INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
        ''')

    def run(self, *, min_fragments=3, min_interval=3600, budget=10, now=None):
        now = time.time() if now is None else now
        fragments = self.memory.db.execute("SELECT COUNT(*) FROM memories WHERE status NOT IN ('invalidated','archived')").fetchone()[0]
        last = self.memory.db.execute("SELECT MAX(started_at) FROM dream_runs WHERE status='completed'").fetchone()[0]
        lock = f"dream_{uuid.uuid4().hex}"
        gate = {"fragments": fragments, "min_fragments": min_fragments, "last_run": last, "min_interval": min_interval, "budget": budget}
        run_id = f"dreamrun_{uuid.uuid4().hex}"
        with self.memory.db:
            try:
                self.memory.db.execute("INSERT INTO dream_lock VALUES (1,?,?)", (lock, now))
            except Exception:
                self.memory.db.execute("INSERT INTO dream_runs VALUES (?,?,?,?,?,?)", (run_id, now, "skipped", json.dumps(gate), 0, "locked"))
                return {"run_id": run_id, "status": "skipped", "reason": "locked", "gate": gate}
            reason = None
            if fragments < min_fragments: reason = "insufficient_fragments"
            elif last is not None and now - last < min_interval: reason = "interval_not_elapsed"
            if reason:
                self.memory.db.execute("INSERT INTO dream_runs VALUES (?,?,?,?,?,?)", (run_id, now, "skipped", json.dumps(gate), 0, reason))
                self.memory.db.execute("DELETE FROM dream_lock WHERE lock_id=1")
                return {"run_id": run_id, "status": "skipped", "reason": reason, "gate": gate}
            rows = self.memory.db.execute("SELECT memory_id, content FROM memories WHERE status IN ('candidate','warm','hot') ORDER BY updated_at DESC LIMIT ?", (budget,)).fetchall()
            candidate_id = f"dream_{uuid.uuid4().hex}"
            content = "\n".join(row["content"] for row in rows)
            self.memory.db.execute("INSERT INTO dream_runs VALUES (?,?,?,?,?,?)", (run_id, now, "completed", json.dumps(gate), 1, None))
            self.memory.db.execute("INSERT INTO dream_candidates VALUES (?,?,?,?,?,?)", (candidate_id, run_id, json.dumps([row["memory_id"] for row in rows]), content, "candidate", now))
            self.memory.db.execute("DELETE FROM dream_lock WHERE lock_id=1")
            return {"run_id": run_id, "status": "completed", "candidate_id": candidate_id, "gate": gate}

    def distill_memories(self, memory_ids, *, summary: str, merged_items=None, conflicts=None, discarded_items=None, information_loss=0.0, reversible=True):
        ids = list(dict.fromkeys(memory_ids))
        if len(ids) < 2 or not summary.strip():
            raise ValueError("distillation_requires_multiple_memories_and_summary")
        if not 0 <= information_loss <= 1:
            raise ValueError("information_loss_out_of_range")
        if not reversible:
            raise ValueError("distillation_must_be_reversible")
        placeholders = ",".join("?" for _ in ids)
        rows = list(self.memory.db.execute(f"SELECT memory_id FROM memories WHERE memory_id IN ({placeholders}) AND status NOT IN ('invalidated','archived')", ids))
        if len(rows) != len(ids):
            raise ValueError("distillation_source_memory_not_found")
        fingerprint = "|".join(sorted(ids)) + "|" + summary.strip()
        distillation_id = f"distill_{uuid.uuid4().hex}"
        now = time.time()
        with self.memory.db:
            try:
                self.memory.db.execute("INSERT INTO memory_distillations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (distillation_id, fingerprint, json.dumps(ids, ensure_ascii=False), summary.strip(), json.dumps(list(merged_items or []), ensure_ascii=False), json.dumps(list(conflicts or []), ensure_ascii=False), json.dumps(list(discarded_items or []), ensure_ascii=False), information_loss, 1, "candidate", now))
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    return dict(self.memory.db.execute("SELECT * FROM memory_distillations WHERE fingerprint=?", (fingerprint,)).fetchone())
                raise
        return dict(self.memory.db.execute("SELECT * FROM memory_distillations WHERE distillation_id=?", (distillation_id,)).fetchone())

    def abstract_memories(self, memory_ids, *, pattern: str, supports=None, counterexamples=None, applicability=None, failure_conditions=None, abstraction_level=1):
        ids = list(dict.fromkeys(memory_ids))
        if len(ids) < 2 or not pattern.strip() or abstraction_level < 1:
            raise ValueError("abstraction_requires_multiple_memories_and_pattern")
        placeholders = ",".join("?" for _ in ids)
        rows = list(self.memory.db.execute(f"SELECT memory_id FROM memories WHERE memory_id IN ({placeholders}) AND status NOT IN ('invalidated','archived')", ids))
        if len(rows) != len(ids):
            raise ValueError("abstraction_source_memory_not_found")
        fingerprint = "|".join(sorted(ids)) + "|" + pattern.strip()
        abstraction_id = f"abstract_{uuid.uuid4().hex}"
        now = time.time()
        with self.memory.db:
            try:
                self.memory.db.execute("INSERT INTO memory_abstractions VALUES (?,?,?,?,?,?,?,?,?,?,?)", (abstraction_id, fingerprint, json.dumps(ids, ensure_ascii=False), pattern.strip(), json.dumps(list(supports or ids), ensure_ascii=False), json.dumps(list(counterexamples or []), ensure_ascii=False), json.dumps(list(applicability or []), ensure_ascii=False), json.dumps(list(failure_conditions or []), ensure_ascii=False), abstraction_level, "candidate", now))
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    return dict(self.memory.db.execute("SELECT * FROM memory_abstractions WHERE fingerprint=?", (fingerprint,)).fetchone())
                raise
        return dict(self.memory.db.execute("SELECT * FROM memory_abstractions WHERE abstraction_id=?", (abstraction_id,)).fetchone())

    def propose_hypothesis(self, candidate_id: str, statement: str, *, prior_reliability: float, argument: str, evidence_refs=None):
        if not 0 <= prior_reliability <= 1:
            raise ValueError("prior_reliability_out_of_range")
        if not statement.strip() or not argument.strip():
            raise ValueError("hypothesis_statement_and_argument_required")
        if not self.memory.db.execute("SELECT 1 FROM dream_candidates WHERE candidate_id=?", (candidate_id,)).fetchone():
            raise ValueError("dream_candidate_not_found")
        hypothesis_id = f"hyp_{uuid.uuid4().hex}"
        now = time.time()
        self.memory.db.execute("INSERT INTO hypotheses VALUES (?,?,?,?,?,?,?,?,?)", (hypothesis_id, candidate_id, statement, prior_reliability, argument, "candidate", json.dumps(list(evidence_refs or []), ensure_ascii=False), now, now))
        return hypothesis_id

    def schedule_verification(self, hypothesis_id: str, description: str, *, cost: float, risk: float, expected_information_gain: float, authorized=False):
        if cost < 0 or not 0 <= risk <= 1 or expected_information_gain < 0:
            raise ValueError("invalid_verification_action_metrics")
        if not self.memory.db.execute("SELECT 1 FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone():
            raise ValueError("hypothesis_not_found")
        action_id = f"act_{uuid.uuid4().hex}"
        now = time.time()
        priority = expected_information_gain / (1 + cost) * (1 - risk)
        status = "queued" if authorized else "awaiting_authorization"
        self.memory.db.execute("INSERT INTO verification_actions VALUES (?,?,?,?,?,?,?,?,?,?)", (action_id, hypothesis_id, description, cost, risk, expected_information_gain, int(authorized), status, now, now))
        if authorized:
            self.memory.db.execute("INSERT INTO verification_queue VALUES (?,?,?,?,?,?,?)", (f"queue_{uuid.uuid4().hex}", hypothesis_id, action_id, priority, "information_gain_cost_risk", "queued", now))
        return {"action_id": action_id, "priority": priority, "status": status}

    def record_verification(self, hypothesis_id: str, *, outcome: str, evidence_refs, new_memory_id: str | None = None, new_content: str | None = None):
        if outcome not in {"supported", "rejected", "unknown", "execution_failed"}:
            raise ValueError("invalid_verification_outcome")
        refs = list(evidence_refs or [])
        if outcome in {"supported", "rejected"} and not refs:
            raise ValueError("verification_evidence_required")
        if not self.memory.db.execute("SELECT 1 FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone():
            raise ValueError("hypothesis_not_found")
        status = "supported" if outcome == "supported" else ("rejected" if outcome == "rejected" else "unknown")
        now = time.time()
        self.memory.db.execute("UPDATE hypotheses SET status=?,evidence_refs=?,updated_at=? WHERE hypothesis_id=?", (status, json.dumps(refs, ensure_ascii=False), now, hypothesis_id))
        self.memory.db.execute("UPDATE verification_actions SET status='completed',updated_at=? WHERE hypothesis_id=? AND status IN ('queued','running')", (now, hypothesis_id))
        if new_memory_id and new_content and outcome == "supported":
            self.memory.put(new_memory_id, "experience", new_content, source_event_ids=refs, status="candidate", change_reason="dream_hypothesis_verified")
        elif new_memory_id and new_content and outcome == "rejected":
            self.memory.put(new_memory_id, "lesson", new_content, source_event_ids=refs, status="candidate", change_reason="dream_hypothesis_rejected")
        return {"hypothesis_id": hypothesis_id, "status": status, "evidence_refs": refs}

    def verification_queue(self, limit=20):
        return [dict(row) for row in self.memory.db.execute("SELECT * FROM verification_queue WHERE status='queued' ORDER BY priority DESC, created_at LIMIT ?", (limit,))]
