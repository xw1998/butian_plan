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
        schema = ('''
        CREATE TABLE IF NOT EXISTS dream_runs (run_id TEXT PRIMARY KEY, started_at REAL NOT NULL, status TEXT NOT NULL, gate_snapshot TEXT NOT NULL, candidate_count INTEGER NOT NULL DEFAULT 0, reason TEXT);
        CREATE TABLE IF NOT EXISTS hypotheses (hypothesis_id TEXT PRIMARY KEY, candidate_id TEXT, statement TEXT NOT NULL, prior_reliability REAL NOT NULL, argument TEXT NOT NULL, status TEXT NOT NULL, evidence_refs TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS verification_actions (action_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, description TEXT NOT NULL, cost REAL NOT NULL, risk REAL NOT NULL, expected_information_gain REAL NOT NULL, authorized INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS verification_queue (queue_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, action_id TEXT NOT NULL, priority REAL NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS dream_candidates (candidate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, memory_ids TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS dream_materials (memory_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, run_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dream_verification_evidence (hypothesis_id TEXT NOT NULL, fingerprint TEXT NOT NULL, action_id TEXT NOT NULL, evidence_ref TEXT NOT NULL, outcome TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(hypothesis_id,fingerprint));
        CREATE TABLE IF NOT EXISTS dream_lock (lock_id INTEGER PRIMARY KEY CHECK(lock_id=1), owner TEXT NOT NULL, acquired_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_distillations (distillation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, source_memory_ids TEXT NOT NULL, source_snapshot TEXT NOT NULL DEFAULT '[]', summary TEXT NOT NULL, merged_items TEXT NOT NULL, conflicts TEXT NOT NULL, discarded_items TEXT NOT NULL, information_loss REAL NOT NULL, reversible INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_abstractions (abstraction_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, source_memory_ids TEXT NOT NULL, supports TEXT NOT NULL, counterexamples TEXT NOT NULL, applicability TEXT NOT NULL, failure_conditions TEXT NOT NULL, abstraction_level INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
        ''')

        for statement in schema.split(";"):
            if statement.strip():
                self.memory.db.execute(statement)

        candidate_columns = {row[1] for row in self.memory.db.execute("PRAGMA table_info(dream_candidates)")}
        if "source_snapshot" not in candidate_columns:
            self.memory.db.execute("ALTER TABLE dream_candidates ADD COLUMN source_snapshot TEXT NOT NULL DEFAULT '[]'")

        distillation_columns = {row[1] for row in self.memory.db.execute("PRAGMA table_info(memory_distillations)")}
        if "source_snapshot" not in distillation_columns:
            self.memory.db.execute("ALTER TABLE memory_distillations ADD COLUMN source_snapshot TEXT NOT NULL DEFAULT '[]'")

        abstraction_columns = {row[1] for row in self.memory.db.execute("PRAGMA table_info(memory_abstractions)")}
        for column, definition in (("pattern", "TEXT NOT NULL DEFAULT ''"), ("source_snapshot", "TEXT NOT NULL DEFAULT '[]'")):
            if column not in abstraction_columns:
                self.memory.db.execute(f"ALTER TABLE memory_abstractions ADD COLUMN {column} {definition}")

    def run(self, *, min_fragments=3, min_interval=3600, budget=10, lock_timeout=7200, now=None):
        now = time.time() if now is None else now
        import math
        if (any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (budget, min_fragments))
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in (min_interval, lock_timeout, now))):
            raise ValueError("invalid_dream_run_limits")
        db = self.memory.db
        db.execute('BEGIN IMMEDIATE')
        try:
            result = self._run_locked(min_fragments, min_interval, budget, lock_timeout, now)
            db.execute('COMMIT')
            return result
        except Exception:
            db.execute('ROLLBACK')
            raise

    def _run_locked(self, min_fragments, min_interval, budget, lock_timeout, now):
        db = self.memory.db
        run_id = f"dreamrun_{uuid.uuid4().hex}"
        stale = db.execute("SELECT acquired_at FROM dream_lock WHERE lock_id=1").fetchone()
        if stale and now - stale[0] >= lock_timeout:
            db.execute("DELETE FROM dream_lock WHERE lock_id=1")
            stale = None
        # Read the gate only after acquiring SQLite's writer lock.
        fragments = db.execute("SELECT COUNT(*) FROM memories WHERE status NOT IN ('invalidated','archived') AND COALESCE(json_extract(environment, '$.origin'), '') != 'butian_initial_asset'").fetchone()[0]
        last = db.execute("SELECT MAX(started_at) FROM dream_runs WHERE status='completed'").fetchone()[0]
        # Imported methods are recallable knowledge, not fresh practical observations.
        materials = list(db.execute("SELECT memory_id,content,status,updated_at FROM memories WHERE status IN ('candidate','warm','hot') AND COALESCE(json_extract(environment, '$.origin'), '') != 'butian_initial_asset' ORDER BY updated_at DESC"))
        fingerprints = {row['memory_id']: hashlib.sha256(row['content'].encode('utf-8')).hexdigest() for row in materials}
        processed = {row['memory_id']: row['content_hash'] for row in db.execute('SELECT memory_id,content_hash FROM dream_materials')}
        new_ids = {mid for mid, digest in fingerprints.items() if processed.get(mid) != digest}
        gate = dict(valid_materials=len(materials), new_fragments=len(new_ids), fragments=fragments, min_fragments=min_fragments, last_run=last, min_interval=min_interval, budget=budget, lock_timeout=lock_timeout)
        reason = None
        if stale: reason = 'locked'
        elif fragments < min_fragments: reason = 'insufficient_fragments'
        elif last is not None and now - last < min_interval: reason = 'interval_not_elapsed'
        elif not materials: reason = 'insufficient_valid_material'
        elif len(new_ids) < min_fragments: reason = 'insufficient_new_fragments'
        if reason:
            db.execute('INSERT INTO dream_runs VALUES (?,?,?,?,?,?)', (run_id, now, 'skipped', json.dumps(gate), 0, reason))
            return dict(run_id=run_id, status='skipped', reason=reason, gate=gate)
        db.execute('INSERT INTO dream_lock VALUES (1,?,?)', (run_id, now))
        rows = [row for row in materials if row['memory_id'] in new_ids][:budget]
        candidate_id = f"dream_{uuid.uuid4().hex}"
        content = '\n'.join(row['content'] for row in rows)
        db.execute('INSERT INTO dream_runs VALUES (?,?,?,?,?,?)', (run_id, now, 'completed', json.dumps(gate), 1, None))
        snapshots = [dict(memory_id=row['memory_id'], content_sha256=fingerprints[row['memory_id']], status=row['status'], updated_at=row['updated_at']) for row in rows]
        db.execute('INSERT INTO dream_candidates(candidate_id,run_id,memory_ids,content,status,created_at,source_snapshot) VALUES (?,?,?,?,?,?,?)', (candidate_id, run_id, json.dumps([row['memory_id'] for row in rows]), content, 'candidate', now, json.dumps(snapshots)))
        for row in rows:
            db.execute('INSERT INTO dream_materials VALUES (?,?,?) ON CONFLICT(memory_id) DO UPDATE SET content_hash=excluded.content_hash,run_id=excluded.run_id', (row['memory_id'], fingerprints[row['memory_id']], run_id))
        db.execute('DELETE FROM dream_lock WHERE lock_id=1 AND owner=?', (run_id,))
        return dict(run_id=run_id, status='completed', candidate_id=candidate_id, gate=gate)

    def distill_memories(self, memory_ids, *, summary: str, merged_items=None, conflicts=None, discarded_items=None, information_loss=0.0, reversible=True):
        db = self.memory.db
        db.execute("BEGIN IMMEDIATE")
        try:
            result = self._distill_memories_locked(memory_ids, summary=summary, merged_items=merged_items, conflicts=conflicts, discarded_items=discarded_items, information_loss=information_loss, reversible=reversible)
            db.execute("COMMIT")
            return result
        except Exception:
            db.execute("ROLLBACK")
            raise

    def _distill_memories_locked(self, memory_ids, *, summary: str, merged_items=None, conflicts=None, discarded_items=None, information_loss=0.0, reversible=True):
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
        fingerprint_data = {"sources": snapshots, "summary": summary.strip(), "merged_items": list(merged_items or []), "conflicts": list(conflicts or []), "discarded_items": list(discarded_items or []), "information_loss": information_loss, "reversible": bool(reversible)}
        fingerprint = hashlib.sha256(json.dumps(fingerprint_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        distillation_id = f"distill_{uuid.uuid4().hex}"
        now = time.time()
        from contextlib import nullcontext
        with nullcontext():
            try:
                self.memory.db.execute("INSERT INTO memory_distillations(distillation_id,fingerprint,source_memory_ids,summary,merged_items,conflicts,discarded_items,information_loss,reversible,status,created_at,source_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (distillation_id, fingerprint, json.dumps(ids, ensure_ascii=False), summary.strip(), json.dumps(list(merged_items or []), ensure_ascii=False), json.dumps(list(conflicts or []), ensure_ascii=False), json.dumps(list(discarded_items or []), ensure_ascii=False), information_loss, 1, "candidate", now, json.dumps(snapshots, ensure_ascii=False)))
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    return dict(self.memory.db.execute("SELECT * FROM memory_distillations WHERE fingerprint=?", (fingerprint,)).fetchone())
                raise
        return dict(self.memory.db.execute("SELECT * FROM memory_distillations WHERE distillation_id=?", (distillation_id,)).fetchone())

    def abstract_memories(self, memory_ids, *, pattern: str, supports=None, counterexamples=None, applicability=None, failure_conditions=None, abstraction_level=1):
        db = self.memory.db
        db.execute("BEGIN IMMEDIATE")
        try:
            result = self._abstract_memories_locked(memory_ids, pattern=pattern, supports=supports, counterexamples=counterexamples, applicability=applicability, failure_conditions=failure_conditions, abstraction_level=abstraction_level)
            db.execute("COMMIT")
            return result
        except Exception:
            db.execute("ROLLBACK")
            raise

    def _abstract_memories_locked(self, memory_ids, *, pattern: str, supports=None, counterexamples=None, applicability=None, failure_conditions=None, abstraction_level=1):
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
        snapshots = []
        for source_id in sorted(set(ids + counterexamples)):
            if source_id in ids or self.memory.db.execute("SELECT 1 FROM memories WHERE memory_id=?", (source_id,)).fetchone():
                source = self.memory.get(source_id)
                snapshots.append({"memory_id": source_id, "content_sha256": hashlib.sha256(source["content"].encode("utf-8")).hexdigest(), "status": source["status"]})
        fingerprint_data = {"sources": snapshots, "pattern": pattern.strip(), "supports": sorted(supports), "counterexamples": sorted(counterexamples), "applicability": list(applicability or []), "failure_conditions": list(failure_conditions or []), "abstraction_level": abstraction_level}
        fingerprint = hashlib.sha256(json.dumps(fingerprint_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        abstraction_id = f"abstract_{uuid.uuid4().hex}"
        now = time.time()
        from contextlib import nullcontext
        with nullcontext():
            try:
                self.memory.db.execute("INSERT INTO memory_abstractions(abstraction_id,fingerprint,source_memory_ids,supports,counterexamples,applicability,failure_conditions,abstraction_level,status,created_at,pattern,source_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (abstraction_id, fingerprint, json.dumps(ids, ensure_ascii=False), json.dumps(supports, ensure_ascii=False), json.dumps(counterexamples, ensure_ascii=False), json.dumps(list(applicability or []), ensure_ascii=False), json.dumps(list(failure_conditions or []), ensure_ascii=False), abstraction_level, "candidate", now, pattern.strip(), json.dumps(snapshots, ensure_ascii=False)))
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    return dict(self.memory.db.execute("SELECT * FROM memory_abstractions WHERE fingerprint=?", (fingerprint,)).fetchone())
                raise
        return dict(self.memory.db.execute("SELECT * FROM memory_abstractions WHERE abstraction_id=?", (abstraction_id,)).fetchone())

    def propose_hypothesis(self, candidate_id: str, statement: str, *, prior_reliability: float, argument: str, evidence_refs=None):
        db = self.memory.db
        db.execute("BEGIN IMMEDIATE")
        try:
            result = self._propose_hypothesis_locked(candidate_id, statement, prior_reliability=prior_reliability, argument=argument, evidence_refs=evidence_refs)
            db.execute("COMMIT")
            return result
        except Exception:
            db.execute("ROLLBACK")
            raise

    def _propose_hypothesis_locked(self, candidate_id: str, statement: str, *, prior_reliability: float, argument: str, evidence_refs=None):
        if not 0 <= prior_reliability <= 1:
            raise ValueError("prior_reliability_out_of_range")
        if not statement.strip() or not argument.strip():
            raise ValueError("hypothesis_statement_and_argument_required")
        self._validate_candidate_sources(candidate_id)
        hypothesis_id = f"hyp_{uuid.uuid4().hex}"
        now = time.time()
        self.memory.db.execute("INSERT INTO hypotheses VALUES (?,?,?,?,?,?,?,?,?)", (hypothesis_id, candidate_id, statement, prior_reliability, argument, "candidate", json.dumps(list(evidence_refs or []), ensure_ascii=False), now, now))
        return hypothesis_id

    def _validate_candidate_sources(self, candidate_id):
        record = None
        for table, key in (("dream_candidates", "candidate_id"), ("memory_abstractions", "abstraction_id"), ("memory_distillations", "distillation_id")):
            record = self.memory.db.execute(f"SELECT source_snapshot FROM {table} WHERE {key}=? AND status='candidate'", (candidate_id,)).fetchone()
            if record:
                break
        if not record:
            raise ValueError("dream_candidate_not_found")
        snapshots = json.loads(record["source_snapshot"])
        if not snapshots:
            raise ValueError("dream_source_snapshot_stale")
        for source in snapshots:
            current = self.memory.get(source["memory_id"])
            if (not current or current["status"] in {"invalidated", "archived"} or
                    hashlib.sha256(current["content"].encode("utf-8")).hexdigest() != source["content_sha256"] or
                    ("status" in source and current["status"] != source["status"]) or
                    ("updated_at" in source and current["updated_at"] != source["updated_at"])):
                raise ValueError("dream_source_snapshot_stale")

    def _validate_hypothesis_sources(self, hypothesis_id):
        hypothesis = self.memory.db.execute("SELECT candidate_id FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone()
        if not hypothesis:
            raise ValueError("hypothesis_not_found")
        self._validate_candidate_sources(hypothesis["candidate_id"])

    def schedule_verification(self, hypothesis_id: str, description: str, *, cost: float, risk: float, expected_information_gain: float, authorized=False):
        import math
        if not isinstance(description, str) or not description.strip() or not isinstance(authorized, bool) or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (cost, risk, expected_information_gain)) or cost < 0 or not 0 <= risk <= 1 or expected_information_gain < 0:
            raise ValueError("invalid_verification_action_metrics")
        if not self.memory.db.execute("SELECT 1 FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone():
            raise ValueError("hypothesis_not_found")
        action_id = f"act_{uuid.uuid4().hex}"
        now = time.time()
        priority = expected_information_gain / (1 + cost) * (1 - risk)
        status = "queued" if authorized else "awaiting_authorization"
        self.memory.db.execute("SAVEPOINT dream_schedule")
        try:
            self.memory.db.execute("INSERT INTO verification_actions VALUES (?,?,?,?,?,?,?,?,?,?)", (action_id, hypothesis_id, description, cost, risk, expected_information_gain, int(authorized), status, now, now))
            if authorized:
                self.memory.db.execute("INSERT INTO verification_queue VALUES (?,?,?,?,?,?,?)", (f"queue_{uuid.uuid4().hex}", hypothesis_id, action_id, priority, "information_gain_cost_risk", "queued", now))
            self.memory.db.execute("RELEASE SAVEPOINT dream_schedule")
        except Exception:
            self.memory.db.execute("ROLLBACK TO SAVEPOINT dream_schedule")
            self.memory.db.execute("RELEASE SAVEPOINT dream_schedule")
            raise
        return {"action_id": action_id, "priority": priority, "status": status}

    def start_verification(self, action_id: str):
        db = self.memory.db
        db.execute("BEGIN IMMEDIATE")
        try:
            action = db.execute("SELECT * FROM verification_actions WHERE action_id=?", (action_id,)).fetchone()
            if not action:
                raise ValueError("verification_action_not_found")
            if not action["authorized"] or action["status"] != "queued":
                raise ValueError("verification_action_not_authorized_or_queued")
            self._validate_hypothesis_sources(action["hypothesis_id"])
            from .exploration_budget import ExplorationBudget
            if self.team_store is None:
                ExplorationBudget(db).reserve(action_id, self.actor_member_id, cost=action["cost"], risk=action["risk"])
            else:
                # Commit the shared reservation before the member DB state.
                # A crash retains the charge; retry uses the same action ID.
                with self.team_store._write_lock:
                    ExplorationBudget(self.team_store.db).reserve(action_id, self.actor_member_id, cost=action["cost"], risk=action["risk"])
            db.execute("UPDATE verification_actions SET status='running',updated_at=? WHERE action_id=?", (time.time(), action_id))
            db.execute("UPDATE verification_queue SET status='running' WHERE action_id=?", (action_id,))
            db.execute("COMMIT")
            return {"action_id": action_id, "status": "running"}
        except Exception:
            db.execute("ROLLBACK")
            raise

    def record_verification(self, hypothesis_id: str, *, outcome: str, evidence_refs, action_id: str | None = None, new_memory_id: str | None = None, new_content: str | None = None):
        db = self.memory.db
        db.execute("BEGIN IMMEDIATE")
        try:
            result = self._record_verification_locked(hypothesis_id, outcome=outcome, evidence_refs=evidence_refs, action_id=action_id, new_memory_id=new_memory_id, new_content=new_content)
            db.execute("COMMIT")
            return result
        except Exception:
            db.execute("ROLLBACK")
            raise

    def _record_verification_locked(self, hypothesis_id: str, *, outcome: str, evidence_refs, action_id: str | None = None, new_memory_id: str | None = None, new_content: str | None = None):
        if outcome not in {"supported", "rejected", "unknown", "execution_failed"}:
            raise ValueError("invalid_verification_outcome")
        if outcome in {"supported", "rejected"} and not self.memory.db.execute("SELECT 1 FROM verification_actions WHERE hypothesis_id=? AND status='running' AND authorized=1", (hypothesis_id,)).fetchone():
            raise ValueError("verification_running_action_required")
        if outcome in {"supported", "rejected"}:
            self._validate_hypothesis_sources(hypothesis_id)
        running = self.memory.db.execute("SELECT action_id FROM verification_actions WHERE hypothesis_id=? AND status='running' AND authorized=1", (hypothesis_id,)).fetchall()
        if action_id is not None:
            action = self.memory.db.execute("SELECT hypothesis_id,status,authorized FROM verification_actions WHERE action_id=?", (action_id,)).fetchone()
            if action is None or action["hypothesis_id"] != hypothesis_id:
                raise ValueError("verification_action_mismatch")
            if action["status"] != "running" or not action["authorized"]:
                raise ValueError("verification_running_action_required")
        elif len(running) > 1:
            raise ValueError("verification_action_ambiguous")
        elif running:
            action_id = running[0]["action_id"]
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
        if outcome in {'supported', 'rejected'}:
            fingerprints = []
            for ref in refs:
                fingerprint = ref
                if self.team_store is not None:
                    evidence = self.team_store.db.execute('SELECT path,sha256 FROM evidence WHERE evidence_id=?', (ref,)).fetchone()
                    fingerprint = evidence['sha256']
                fingerprints.append((fingerprint, ref))
            if len({item[0] for item in fingerprints}) != len(fingerprints):
                raise ValueError('verification_evidence_not_independent')
            previous = self.memory.db.execute('SELECT evidence_refs FROM hypotheses WHERE hypothesis_id=?', (hypothesis_id,)).fetchone()
            previous_refs = set(json.loads(previous['evidence_refs']))
            if self.team_store is not None:
                historic_refs = previous_refs | {row[0] for row in self.memory.db.execute('SELECT evidence_ref FROM dream_verification_evidence WHERE hypothesis_id=?', (hypothesis_id,))}
                historic_hashes = set()
                for historic_ref in historic_refs:
                    historic = self.team_store.db.execute('SELECT sha256 FROM evidence WHERE evidence_id=?', (historic_ref,)).fetchone()
                    if historic is not None:
                        historic_hashes.add(historic['sha256'])
                if any(fingerprint in historic_hashes for fingerprint, _ in fingerprints):
                    raise ValueError('verification_evidence_not_independent')
            for fingerprint, ref in fingerprints:
                if ref in previous_refs or self.memory.db.execute('SELECT 1 FROM dream_verification_evidence WHERE hypothesis_id=? AND fingerprint=?', (hypothesis_id, fingerprint)).fetchone():
                    raise ValueError('verification_evidence_not_independent')
            for fingerprint, ref in fingerprints:
                self.memory.db.execute('INSERT INTO dream_verification_evidence VALUES (?,?,?,?,?,?)', (hypothesis_id, fingerprint, action_id, ref, outcome, time.time()))
        status = "supported" if outcome == "supported" else ("rejected" if outcome == "rejected" else "unknown")
        now = time.time()
        self.memory.db.execute("UPDATE hypotheses SET status=?,evidence_refs=?,updated_at=? WHERE hypothesis_id=?", (status, json.dumps(refs, ensure_ascii=False), now, hypothesis_id))
        if action_id is not None:
            self.memory.db.execute("UPDATE verification_actions SET status='completed',updated_at=? WHERE action_id=? AND status='running'", (now, action_id))
            self.memory.db.execute("UPDATE verification_queue SET status='completed' WHERE status='running' AND action_id=?", (action_id,))
        if new_memory_id and new_content and outcome == "supported":
            self.memory.put(new_memory_id, "experience", new_content, source_event_ids=refs, status="candidate", change_reason="dream_hypothesis_verified")
        elif new_memory_id and new_content and outcome == "rejected":
            self.memory.put(new_memory_id, "lesson", new_content, source_event_ids=refs, status="candidate", change_reason="dream_hypothesis_rejected")
        return {"hypothesis_id": hypothesis_id, "status": status, "evidence_refs": refs}

    def verification_queue(self, limit=20):
        return [dict(row) for row in self.memory.db.execute("SELECT * FROM verification_queue WHERE status='queued' ORDER BY priority DESC, created_at LIMIT ?", (limit,))]
