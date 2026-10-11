from __future__ import annotations
import hashlib
import json, time, uuid
from pathlib import Path
from .memory import MemoryStore

class DreamRunner:
    def __init__(self, memory: MemoryStore, *, team_store=None, actor_member_id: str | None = None):
        self.memory = memory
        self.team_store = team_store
        self.actor_member_id = actor_member_id
        self.memory.db.executescript('''
        CREATE TABLE IF NOT EXISTS dream_runs (run_id TEXT PRIMARY KEY, started_at REAL NOT NULL, status TEXT NOT NULL, gate_snapshot TEXT NOT NULL, candidate_count INTEGER NOT NULL DEFAULT 0, reason TEXT);
        CREATE TABLE IF NOT EXISTS hypotheses (hypothesis_id TEXT PRIMARY KEY, candidate_id TEXT, statement TEXT NOT NULL, prior_reliability REAL NOT NULL, argument TEXT NOT NULL, status TEXT NOT NULL, evidence_refs TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS verification_actions (action_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, description TEXT NOT NULL, cost REAL NOT NULL, risk REAL NOT NULL, expected_information_gain REAL NOT NULL, authorized INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS verification_queue (queue_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, action_id TEXT NOT NULL, priority REAL NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS dream_candidates (candidate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, memory_ids TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS dream_lock (lock_id INTEGER PRIMARY KEY CHECK(lock_id=1), owner TEXT NOT NULL, acquired_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_distillations (distillation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, source_memory_ids TEXT NOT NULL, source_snapshot TEXT NOT NULL DEFAULT '[]', summary TEXT NOT NULL, merged_items TEXT NOT NULL, conflicts TEXT NOT NULL, discarded_items TEXT NOT NULL, information_loss REAL NOT NULL, reversible INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_abstractions (abstraction_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, source_memory_ids TEXT NOT NULL, supports TEXT NOT NULL, counterexamples TEXT NOT NULL, applicability TEXT NOT NULL, failure_conditions TEXT NOT NULL, abstraction_level INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
        ''')

        distillation_columns = {row[1] for row in self.memory.db.execute("PRAGMA table_info(memory_distillations)")}
        if "source_snapshot" not in distillation_columns:
            self.memory.db.execute("ALTER TABLE memory_distillations ADD COLUMN source_snapshot TEXT NOT NULL DEFAULT '[]'")

    def run(self, *, min_fragments=3, min_interval=3600, budget=10, lock_timeout=7200, now=None):
        now = time.time() if now is None else now
        fragments = self.memory.db.execute("SELECT COUNT(*) FROM memories WHERE status NOT IN ('invalidated','archived')").fetchone()[0]
        last = self.memory.db.execute("SELECT MAX(started_at) FROM dream_runs WHERE status='completed'").fetchone()[0]
        lock = f"dream_{uuid.uuid4().hex}"
        if budget < 1 or min_fragments < 1 or min_interval < 0 or lock_timeout < 0:
            raise ValueError("invalid_dream_run_limits")
        gate = {"fragments": fragments, "min_fragments": min_fragments, "last_run": last, "min_interval": min_interval, "budget": budget, "lock_timeout": lock_timeout}
        run_id = f"dreamrun_{uuid.uuid4().hex}"
        with self.memory.db:
            try:
                stale = self.memory.db.execute("SELECT acquired_at FROM dream_lock WHERE lock_id=1").fetchone()
                if stale and now - stale[0] >= lock_timeout:
                    self.memory.db.execute("DELETE FROM dream_lock WHERE lock_id=1")
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
        snapshots = []
        for item in sorted(rows, key=lambda row: row["memory_id"]):
            source = self.memory.get(item["memory_id"])
            snapshots.append({"memory_id": source["memory_id"], "updated_at": source["updated_at"], "content_sha256": hashlib.sha256(source["content"].encode("utf-8")).hexdigest()})
        fingerprint = hashlib.sha256((json.dumps(snapshots, sort_keys=True) + "|" + summary.strip()).encode("utf-8")).hexdigest()
        distillation_id = f"distill_{uuid.uuid4().hex}"
        now = time.time()
        with self.memory.db:
            try:
                self.memory.db.execute("INSERT INTO memory_distillations(distillation_id,fingerprint,source_memory_ids,summary,merged_items,conflicts,discarded_items,information_loss,reversible,status,created_at,source_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (distillation_id, fingerprint, json.dumps(ids, ensure_ascii=False), summary.strip(), json.dumps(list(merged_items or []), ensure_ascii=False), json.dumps(list(conflicts or []), ensure_ascii=False), json.dumps(list(discarded_items or []), ensure_ascii=False), information_loss, 1, "candidate", now, json.dumps(snapshots, ensure_ascii=False)))
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    return dict(self.memory.db.execute("SELECT * FROM memory_distillations WHERE fingerprint=?", (fingerprint,)).fetchone())
                raise
        return dict(self.memory.db.execute("SELECT * FROM memory_distillations WHERE distillation_id=?", (distillation_id,)).fetchone())

    def abstract_memories(self, memory_ids, *, pattern: str, supports=None, counterexamples=None, applicability=None, failure_conditions=None, abstraction_level=1):
        ids = list(dict.fromkeys(memory_ids))
        if len(ids) < 2 or not pattern.strip() or abstraction_level < 1:
            raise ValueError("abstraction_requires_multiple_memories_and_pattern")
        if supports is not None and not set(supports).issubset(set(ids)):
            raise ValueError("abstraction_support_must_be_source_memory")
        placeholders = ",".join("?" for _ in ids)
        rows = list(self.memory.db.execute(f"SELECT memory_id FROM memories WHERE memory_id IN ({placeholders}) AND status NOT IN ('invalidated','archived')", ids))
        if len(rows) != len(ids):
            raise ValueError("abstraction_source_memory_not_found")
        supports = list(ids if supports is None else supports)
        counterexamples = list(counterexamples or [])
        if counterexamples:
            known_memory_ids = {row[0] for row in self.memory.db.execute("SELECT memory_id FROM memories")}
            known_events = set()
            for row in self.memory.db.execute("SELECT source_event_ids FROM memories"):
                known_events.update(json.loads(row[0]))
            unknown_ce = set(counterexamples) - known_memory_ids - known_events
            if unknown_ce:
                raise ValueError("abstraction_counterexample_not_found")
        fingerprint = hashlib.sha256((json.dumps(sorted(ids)) + "|" + pattern.strip() + "|" + json.dumps(sorted(supports)) + "|" + json.dumps(sorted(counterexamples))).encode("utf-8")).hexdigest()
        abstraction_id = f"abstract_{uuid.uuid4().hex}"
        now = time.time()
        with self.memory.db:
            try:
                self.memory.db.execute("INSERT INTO memory_abstractions(abstraction_id,fingerprint,source_memory_ids,supports,counterexamples,applicability,failure_conditions,abstraction_level,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (abstraction_id, fingerprint, json.dumps(ids, ensure_ascii=False), json.dumps(supports, ensure_ascii=False), json.dumps(counterexamples, ensure_ascii=False), json.dumps(list(applicability or []), ensure_ascii=False), json.dumps(list(failure_conditions or []), ensure_ascii=False), abstraction_level, "candidate", now))
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
        if outcome in {"supported", "rejected"} and self.team_store is not None:
            if not all(isinstance(ref, str) and ref.startswith("ev_") for ref in refs):
                raise ValueError("team_evidence_id_required")
            for evidence_id in refs:
                verified = self.team_store.verify_evidence(evidence_id, actor_member_id=self.actor_member_id)
                if verified["status"] != "valid":
                    raise ValueError("verification_evidence_not_valid")
        if not self.memory.db.execute("SELECT 1 FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone():
            raise ValueError("hypothesis_not_found")
        status = "supported" if outcome == "supported" else ("rejected" if outcome == "rejected" else "unknown")
        now = time.time()
        self.memory.db.execute("UPDATE hypotheses SET status=?,evidence_refs=?,updated_at=? WHERE hypothesis_id=?", (status, json.dumps(refs, ensure_ascii=False), now, hypothesis_id))
        self.memory.db.execute("UPDATE verification_actions SET status='completed',updated_at=? WHERE hypothesis_id=? AND status='running'", (now, hypothesis_id))
        self.memory.db.execute("UPDATE verification_queue SET status='completed' WHERE status='queued' AND action_id IN (SELECT action_id FROM verification_actions WHERE hypothesis_id=? AND status='completed')", (hypothesis_id,) )
        if new_memory_id and new_content and outcome == "supported":
            self.memory.put(new_memory_id, "experience", new_content, source_event_ids=refs, status="candidate", change_reason="dream_hypothesis_verified")
        elif new_memory_id and new_content and outcome == "rejected":
            self.memory.put(new_memory_id, "lesson", new_content, source_event_ids=refs, status="candidate", change_reason="dream_hypothesis_rejected")
        return {"hypothesis_id": hypothesis_id, "status": status, "evidence_refs": refs}

    def verification_queue(self, limit=20):
        return [dict(row) for row in self.memory.db.execute("SELECT * FROM verification_queue WHERE status='queued' ORDER BY priority DESC, created_at LIMIT ?", (limit,))]
