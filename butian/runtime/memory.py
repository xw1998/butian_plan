from __future__ import annotations
import json
import sqlite3
import time
import uuid
from pathlib import Path
from .security import assert_safe_memory_content
from ..core.experience import SkillPhase, SkillState, composition_gain, transition_phase
from ..core.degradation import assess_degradation, environment_drift

SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS memories (memory_id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL, confidence REAL NOT NULL, weight REAL NOT NULL, environment TEXT NOT NULL, source_event_ids TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, recall_count INTEGER NOT NULL DEFAULT 0, useful_count INTEGER NOT NULL DEFAULT 0, harmful_count INTEGER NOT NULL DEFAULT 0, last_recalled_at REAL, last_used_at REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, content);
CREATE TABLE IF NOT EXISTS memory_events (event_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind, status);
CREATE TABLE IF NOT EXISTS memory_versions (version_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, version INTEGER NOT NULL, parent_version_id TEXT, content TEXT NOT NULL, environment TEXT NOT NULL, change_reason TEXT, evidence_refs TEXT NOT NULL DEFAULT '[]', version_status TEXT NOT NULL DEFAULT 'candidate', created_at REAL NOT NULL, UNIQUE(memory_id, version));
CREATE TABLE IF NOT EXISTS memory_conflicts (conflict_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, conflicting_memory_id TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_recall_log (recall_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, query TEXT NOT NULL, layer TEXT NOT NULL, used INTEGER NOT NULL, score REAL NOT NULL, created_at REAL NOT NULL, decision_id TEXT);
CREATE TABLE IF NOT EXISTS memory_recall_decisions (recall_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, query TEXT NOT NULL, outcome TEXT NOT NULL, missed INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_validation_batches (batch_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, state TEXT NOT NULL, sample_count INTEGER NOT NULL DEFAULT 0, pass_count INTEGER NOT NULL DEFAULT 0, fail_count INTEGER NOT NULL DEFAULT 0, evidence_refs TEXT NOT NULL DEFAULT '[]', notes TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_maintenance (maintenance_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_compositions (composition_id TEXT PRIMARY KEY, result_memory_id TEXT NOT NULL, component_memory_ids TEXT NOT NULL, component_fingerprint TEXT NOT NULL UNIQUE, composition_bonus REAL NOT NULL, redundancy_penalty REAL NOT NULL, conflict_penalty REAL NOT NULL, observed_successes INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS skill_states (skill_domain TEXT PRIMARY KEY, phase TEXT NOT NULL, level REAL NOT NULL, abstraction_level INTEGER NOT NULL, marginal_gain REAL NOT NULL, plateau_score REAL NOT NULL, activation_count INTEGER NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS skill_events (event_id TEXT PRIMARY KEY, skill_domain TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_degradation (memory_id TEXT PRIMARY KEY, base_weight REAL NOT NULL, base_confidence REAL NOT NULL, base_status TEXT NOT NULL, score REAL NOT NULL, freshness REAL NOT NULL, state TEXT NOT NULL, reasons TEXT NOT NULL, model_version TEXT NOT NULL, assessed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS practice_observations (observation_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, independent_key TEXT NOT NULL, outcome TEXT NOT NULL, evidence_ref TEXT, environment TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(memory_id, independent_key));
CREATE TABLE IF NOT EXISTS practice_evidence (memory_id TEXT NOT NULL, evidence_digest TEXT NOT NULL, independent_key TEXT NOT NULL, PRIMARY KEY(memory_id,evidence_digest));
CREATE TABLE IF NOT EXISTS recall_feedback_commits (recall_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage_feedback_evidence (memory_id TEXT NOT NULL, evidence_digest TEXT NOT NULL, outcome TEXT NOT NULL, PRIMARY KEY(memory_id,evidence_digest));
'''

class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        degradation_columns = {row[1] for row in self.db.execute("PRAGMA table_info(memory_degradation)")}
        if degradation_columns and "base_status" not in degradation_columns:
            self.db.execute("ALTER TABLE memory_degradation ADD COLUMN base_status TEXT NOT NULL DEFAULT 'warm'")
        memory_columns = {row[1] for row in self.db.execute("PRAGMA table_info(memories)")}
        for column, definition in (("recall_count", "INTEGER NOT NULL DEFAULT 0"), ("useful_count", "INTEGER NOT NULL DEFAULT 0"), ("harmful_count", "INTEGER NOT NULL DEFAULT 0"), ("last_recalled_at", "REAL"), ("last_used_at", "REAL")):
            if column not in memory_columns:
                self.db.execute(f"ALTER TABLE memories ADD COLUMN {column} {definition}")
        recall_columns = {row[1] for row in self.db.execute("PRAGMA table_info(memory_recall_log)")}
        if "decision_id" not in recall_columns:
            self.db.execute("ALTER TABLE memory_recall_log ADD COLUMN decision_id TEXT")
        version_columns = {row[1] for row in self.db.execute("PRAGMA table_info(memory_versions)")}
        if version_columns and "evidence_refs" not in version_columns:
            self.db.execute("ALTER TABLE memory_versions ADD COLUMN evidence_refs TEXT NOT NULL DEFAULT '[]'")
            self.db.execute("ALTER TABLE memory_versions ADD COLUMN version_status TEXT NOT NULL DEFAULT 'candidate'")

    def close(self):
        self.db.close()

    def put(self, memory_id: str, kind: str, content: str, *, confidence=0.0, weight=1.0, environment=None, source_event_ids=None, status="candidate", source_validator=None, parent_version_id=None, change_reason=None, validation_state=None, knowledge_source=None):
        environment = environment or {}
        source_event_ids = list(source_event_ids or [])
        assert_safe_memory_content(memory_id, kind, content, json.dumps(environment, ensure_ascii=False))
        if validation_state is not None and validation_state not in {"pending_prototype", "prototype_received", "batch_validating", "validated", "rejected", "inconclusive"}:
            raise ValueError("invalid_validation_state")
        if validation_state in {"pending_prototype", "prototype_received", "batch_validating"}:
            if status in {"warm", "hot"}:
                raise ValueError("unvalidated_hardware_hypothesis_cannot_be_verified")
            status = "pending_validation"
        if status in {"warm", "hot"} and not source_event_ids:
            raise ValueError("evidence_required_for_verified_memory")
        if source_event_ids and source_validator and not source_validator(source_event_ids):
            raise ValueError("invalid_memory_source_events")
        now = time.time()
        version_id = f"ver_{uuid.uuid4().hex}"
        from contextlib import nullcontext
        with (nullcontext() if self.db.in_transaction else self.db):
            previous = self.db.execute("SELECT content FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            latest = self.db.execute("SELECT version, version_id FROM memory_versions WHERE memory_id=? ORDER BY version DESC LIMIT 1", (memory_id,)).fetchone()
            if parent_version_id is None and latest:
                parent_version_id = latest["version_id"]
            if parent_version_id:
                parent = self.db.execute("SELECT memory_id FROM memory_versions WHERE version_id=?", (parent_version_id,)).fetchone()
                if not parent or parent["memory_id"] != memory_id:
                    raise ValueError("invalid_memory_version_parent")
            version = int(latest["version"] if latest else 0) + 1
            self.db.execute("INSERT INTO memories(memory_id,kind,content,status,confidence,weight,environment,source_event_ids,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET kind=excluded.kind,content=excluded.content,status=excluded.status,confidence=excluded.confidence,weight=excluded.weight,environment=excluded.environment,source_event_ids=excluded.source_event_ids,updated_at=excluded.updated_at", (memory_id, kind, content, status, confidence, weight, json.dumps(environment, ensure_ascii=False), json.dumps(source_event_ids, ensure_ascii=False), now, now))
            self.db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
            self.db.execute("INSERT INTO memory_fts(memory_id,content) VALUES (?,?)", (memory_id, content))
            self.db.execute("INSERT INTO memory_versions(version_id,memory_id,version,parent_version_id,content,environment,change_reason,evidence_refs,version_status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (version_id, memory_id, version, parent_version_id, content, json.dumps(environment, ensure_ascii=False), change_reason, json.dumps(source_event_ids, ensure_ascii=False), status, now))
            if validation_state:
                self.db.execute("INSERT INTO memory_events VALUES (?,?,?,?,?)", (f"evt_{uuid.uuid4().hex}", memory_id, "validation_state", json.dumps({"state": validation_state, "knowledge_source": knowledge_source}, ensure_ascii=False), now))
            if previous and previous["content"] != content:
                conflict = self.db.execute("SELECT memory_id FROM memories WHERE memory_id!=? AND content=? AND status NOT IN ('invalidated','archived') LIMIT 1", (memory_id, content)).fetchone()
                if conflict:
                    self.db.execute("INSERT INTO memory_conflicts VALUES (?,?,?,?,?,?)", (f"conf_{uuid.uuid4().hex}", memory_id, conflict["memory_id"], "same_content_different_memory", "open", now))
        return version_id

    def create_validation_batch(self, memory_id: str, *, sample_count: int, notes: str | None = None, batch_id: str | None = None):
        if sample_count < 1:
            raise ValueError("validation_sample_count_must_be_positive")
        if not self.get(memory_id):
            raise ValueError("memory_not_found")
        now = time.time()
        batch_id = batch_id or f"batch_{uuid.uuid4().hex}"
        with self.db:
            self.db.execute("INSERT INTO memory_validation_batches(batch_id,memory_id,state,sample_count,notes,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (batch_id, memory_id, "pending_prototype", sample_count, notes, now, now))
            self.db.execute("UPDATE memories SET status='pending_validation' WHERE memory_id=? AND status='candidate'", (memory_id,))
        return batch_id

    def update_validation_batch(self, batch_id: str, state: str, *, pass_count: int = 0, fail_count: int = 0, evidence_refs=None, notes: str | None = None):
        allowed = {"pending_prototype", "prototype_received", "batch_validating", "validated", "rejected", "inconclusive"}
        if state not in allowed:
            raise ValueError("invalid_validation_state")
        row = self.db.execute("SELECT * FROM memory_validation_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not row:
            raise ValueError("validation_batch_not_found")
        transitions = {"pending_prototype": {"prototype_received", "inconclusive"}, "prototype_received": {"batch_validating", "inconclusive"}, "batch_validating": {"validated", "rejected", "inconclusive"}, "inconclusive": {"prototype_received", "batch_validating"}, "validated": set(), "rejected": set()}
        if state != row["state"] and state not in transitions[row["state"]]:
            raise ValueError("invalid_validation_transition")
        if pass_count < 0 or fail_count < 0 or pass_count + fail_count > row["sample_count"]:
            raise ValueError("invalid_validation_counts")
        if state == "validated" and (pass_count + fail_count != row["sample_count"] or fail_count != 0 or not evidence_refs):
            raise ValueError("validated_requires_complete_batch_evidence")
        now = time.time()
        self.db.execute("UPDATE memory_validation_batches SET state=?,pass_count=?,fail_count=?,evidence_refs=?,notes=?,updated_at=? WHERE batch_id=?", (state, pass_count, fail_count, json.dumps(list(evidence_refs or []), ensure_ascii=False), notes, now, batch_id))
        if state == "validated":
            self.db.execute("UPDATE memories SET status='warm' WHERE memory_id=? AND status IN ('candidate','pending_validation')", (row["memory_id"],))
            self.db.execute("UPDATE memory_versions SET version_status='warm' WHERE memory_id=? AND version=(SELECT MAX(version) FROM memory_versions WHERE memory_id=?)", (row["memory_id"], row["memory_id"]))
        elif state == "rejected":
            self.db.execute("UPDATE memories SET status='disputed' WHERE memory_id=? AND status='pending_validation'", (row["memory_id"],))
        return dict(self.db.execute("SELECT * FROM memory_validation_batches WHERE batch_id=?", (batch_id,)).fetchone())

    def list_validation_batches(self, memory_id: str):
        return [dict(row) for row in self.db.execute("SELECT * FROM memory_validation_batches WHERE memory_id=? ORDER BY created_at", (memory_id,))]

    def get(self, memory_id: str):
        row = self.db.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        return dict(row) if row else None

    def list_versions(self, memory_id: str):
        return [dict(row) for row in self.db.execute("SELECT * FROM memory_versions WHERE memory_id=? ORDER BY version", (memory_id,))]

    def audit_version_chain(self, memory_id: str):
        versions = self.list_versions(memory_id)
        for index, item in enumerate(versions):
            expected_parent = versions[index - 1]["version_id"] if index else None
            if item["version"] != index + 1 or item["parent_version_id"] != expected_parent:
                raise ValueError("memory_version_chain_corrupt")
            refs = json.loads(item["evidence_refs"])
            if not isinstance(refs, list):
                raise ValueError("memory_version_evidence_corrupt")
        latest = self.get(memory_id)
        if versions and (not latest or latest["content"] != versions[-1]["content"]):
            raise ValueError("memory_current_version_mismatch")
        return {"memory_id": memory_id, "versions": len(versions), "valid": True}

    def search(self, query: str, *, kind=None, limit=20, environment=None, include_cold=True, used=False):
        sql = "SELECT m.* FROM memory_fts f JOIN memories m ON m.memory_id=f.memory_id WHERE f.content MATCH ? AND m.status NOT IN ('invalidated','archived')"
        args: list[object] = [query]
        if kind:
            sql += " AND m.kind=?"; args.append(kind)
        if not include_cold:
            sql += " AND m.status!='cold'"
        sql += " ORDER BY CASE m.status WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 WHEN 'candidate' THEN 2 WHEN 'disputed' THEN 3 ELSE 4 END, m.weight DESC, m.updated_at DESC LIMIT ?"; args.append(limit)
        try:
            rows = list(self.db.execute(sql, args))
        except sqlite3.OperationalError:
            rows = []
        if not rows:
            fallback = "SELECT * FROM memories WHERE content LIKE ? AND status NOT IN ('invalidated','archived')"
            fallback_args: list[object] = [f"%{query}%"]
            if kind:
                fallback += " AND kind=?"; fallback_args.append(kind)
            if not include_cold: fallback += " AND status!='cold'"
            fallback += " ORDER BY CASE status WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 WHEN 'candidate' THEN 2 WHEN 'disputed' THEN 3 ELSE 4 END, weight DESC, updated_at DESC LIMIT ?"; fallback_args.append(limit)
            rows = list(self.db.execute(fallback, fallback_args))
        result = [dict(row) for row in rows]
        now = time.time()
        with self.db:
            for item in result:
                layer = "cold" if item["status"] == "cold" else ("candidate" if item["status"] == "candidate" else "active")
                self.db.execute("INSERT INTO memory_recall_log(recall_id,memory_id,query,layer,used,score,created_at) VALUES (?,?,?,?,?,?,?)", (f"rec_{uuid.uuid4().hex}", item["memory_id"], query, layer, int(used), float(item["weight"]), now))
                self.db.execute("UPDATE memories SET recall_count=recall_count+1,last_recalled_at=? WHERE memory_id=?", (now, item["memory_id"]))
                item["recall_count"] = int(item.get("recall_count", 0)) + 1
                item["last_recalled_at"] = now
        return result

    def recall_for_decision(self, query: str, *, limit=20, environment=None, decision_id=None):
        tokens = list(dict.fromkeys(token for token in query.split() if len(token) >= 2))
        if len(tokens) <= 1 and len(query.strip()) >= 2:
            normalized = query.strip().replace(" ", "")
            tokens = list(dict.fromkeys(normalized[index:index + 2] for index in range(len(normalized) - 1)))
        if not tokens:
            return []
        clauses = " OR ".join("content LIKE ?" for _ in tokens)
        args: list[object] = [f"%{token}%" for token in tokens]
        args.extend([limit])
        rows = list(self.db.execute(f"SELECT * FROM memories WHERE ({clauses}) AND status NOT IN ('invalidated','archived') ORDER BY CASE status WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 WHEN 'candidate' THEN 2 WHEN 'disputed' THEN 3 ELSE 4 END, weight DESC, updated_at DESC LIMIT ?", args))
        result = [dict(row) for row in rows]
        now = time.time()
        with self.db:
            for item in result:
                self.db.execute("INSERT INTO memory_recall_log(recall_id,memory_id,query,layer,used,score,created_at,decision_id) VALUES (?,?,?,?,?,?,?,?)", (f"rec_{uuid.uuid4().hex}", item["memory_id"], query, "decision", 0, float(item["weight"]), now, decision_id))
                self.db.execute("UPDATE memories SET recall_count=recall_count+1,last_recalled_at=? WHERE memory_id=?", (now, item["memory_id"]))
                item["recall_count"] = int(item.get("recall_count", 0)) + 1
                item["last_recalled_at"] = now
        return result

    def record_practice_outcome(self, *, memory_id: str, kind: str, content: str, outcome: str, independent_key: str, evidence_ref: str | None = None, environment=None, source_event_ids=None, evidence_digest: str | None = None):
        """Record independent evidence; contradictions remain disputed pending revision."""
        if outcome not in {"success", "failure", "unknown"}:
            raise ValueError("invalid_practice_outcome")
        if not independent_key.strip():
            raise ValueError("independent_key_required")
        if outcome == "success" and not evidence_ref:
            raise ValueError("practice_success_evidence_required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT 1 FROM practice_observations WHERE memory_id=? AND independent_key=?", (memory_id, independent_key)).fetchone():
                raise ValueError("duplicate_independent_observation")
            if outcome == "success" and self.db.execute("SELECT 1 FROM practice_observations WHERE memory_id=? AND evidence_ref=? AND outcome='success'", (memory_id, evidence_ref)).fetchone():
                raise ValueError("independent_evidence_required")
            if evidence_digest:
                if self.db.execute("SELECT 1 FROM practice_evidence WHERE memory_id=? AND evidence_digest=?", (memory_id, evidence_digest)).fetchone():
                    raise ValueError("independent_evidence_required")
                self.db.execute("INSERT INTO practice_evidence VALUES (?,?,?)", (memory_id, evidence_digest, independent_key))
            row = self.get(memory_id)
            if not row:
                self.put(memory_id, kind, content, confidence=0.2, weight=0.5, environment=environment, source_event_ids=list(source_event_ids or []), status="candidate", change_reason="practice_observation")
                row = self.get(memory_id)
            self.db.execute("INSERT INTO practice_observations VALUES (?,?,?,?,?,?,?)", (f"obs_{uuid.uuid4().hex}", memory_id, independent_key, outcome, evidence_ref, json.dumps(environment or {}, ensure_ascii=False), time.time()))
            successes = self.db.execute("SELECT COUNT(*) FROM practice_observations WHERE memory_id=? AND outcome='success' AND evidence_ref IS NOT NULL", (memory_id,)).fetchone()[0]
            failures = self.db.execute("SELECT COUNT(*) FROM practice_observations WHERE memory_id=? AND outcome='failure'", (memory_id,)).fetchone()[0]
            status, confidence, weight = row["status"], float(row["confidence"]), float(row["weight"])
            if outcome == "failure":
                status = "disputed" if status in {"warm", "hot", "disputed"} else "candidate"
                confidence, weight = max(0.0, confidence - 0.1), max(0.0, weight - 0.1)
            elif outcome == "success" and successes >= 2 and not failures and status in {"candidate", "pending_validation"}:
                status, confidence, weight = "warm", min(1.0, confidence + 0.1), min(5.0, weight + 0.25)
            refs = list(dict.fromkeys(json.loads(row["source_event_ids"]) + list(source_event_ids or []) + ([evidence_ref] if evidence_ref else [])))
            self.put(memory_id, row["kind"], row["content"], status=status, confidence=confidence, weight=weight, environment=json.loads(row["environment"]), source_event_ids=refs, change_reason="practice_" + outcome)
            domain = "practice:" + row["kind"]
            old_skill = self.db.execute("SELECT level FROM skill_states WHERE skill_domain=?", (domain,)).fetchone()
            domain_counts = self.db.execute("SELECT SUM(p.outcome='success'), SUM(p.outcome='failure') FROM practice_observations p JOIN memories m ON m.memory_id=p.memory_id WHERE m.kind=?", (row["kind"],)).fetchone()
            domain_successes, domain_failures = int(domain_counts[0] or 0), int(domain_counts[1] or 0)
            level = domain_successes / (domain_successes + domain_failures + 5)
            marginal = max(0.0, level - (float(old_skill[0]) if old_skill else 0.0))
            self.update_skill(domain, level=level, marginal=marginal, contradiction=outcome == "failure", independent_successes=domain_successes, regression_evidence=outcome == "failure")
            self.db.commit()
            return {"memory_id": memory_id, "status": status, "successes": successes, "failures": failures, "skill_domain": domain}
        except Exception:
            self.db.rollback()
            raise

    def record_usage_feedback(self, memory_id: str, *, outcome: str, evidence_ref: str | None = None, evidence_digest: str | None = None):
        if outcome not in {"useful", "harmful", "unused", "unknown"}:
            raise ValueError("invalid_memory_usage_outcome")
        if outcome in {"useful", "harmful"} and not evidence_ref:
            raise ValueError("usage_feedback_evidence_required")
        own_transaction = not self.db.in_transaction
        if own_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.get(memory_id)
            if not row:
                raise ValueError("memory_not_found")
            if evidence_digest and outcome in {"useful", "harmful"}:
                prior = self.db.execute("SELECT outcome FROM usage_feedback_evidence WHERE memory_id=? AND evidence_digest=?", (memory_id, evidence_digest)).fetchone()
                if prior:
                    if prior["outcome"] != outcome:
                        raise ValueError("usage_feedback_evidence_conflict")
                    if own_transaction:
                        self.db.commit()
                    return row
                self.db.execute("INSERT INTO usage_feedback_evidence VALUES (?,?,?)", (memory_id, evidence_digest, outcome))
            now = time.time()
            if outcome in {"useful", "harmful"}:
                weight = min(5.0, float(row["weight"]) + 0.1) if outcome == "useful" else max(0.0, float(row["weight"]) - 0.25)
                status = ("warm" if row["status"] == "cold" else row["status"]) if outcome == "useful" else ("disputed" if row["status"] in {"warm", "hot"} else row["status"])
                self.put(memory_id, row["kind"], row["content"], status=status, weight=weight, confidence=row["confidence"], environment=json.loads(row["environment"]), source_event_ids=list(dict.fromkeys(json.loads(row["source_event_ids"]) + [evidence_ref])), change_reason="usage_" + outcome)
                column = "useful_count" if outcome == "useful" else "harmful_count"
                self.db.execute(f"UPDATE memories SET {column}={column}+1,last_used_at=? WHERE memory_id=?", (now, memory_id))
            elif outcome == "unused":
                self.db.execute("UPDATE memories SET last_used_at=? WHERE memory_id=?", (now, memory_id))
            self.db.execute("INSERT INTO memory_events VALUES (?,?,?,?,?)", (f"evt_{uuid.uuid4().hex}", memory_id, "usage_feedback", json.dumps({"outcome": outcome, "evidence_ref": evidence_ref}, ensure_ascii=False), now))
            if own_transaction:
                self.db.commit()
            return self.get(memory_id)
        except BaseException:
            if own_transaction:
                self.db.rollback()
            raise

    def record_decision_recall(self, recall_id: str, decision_id: str, query: str, outcome: str, missed: bool):
        self.db.execute("INSERT INTO memory_recall_decisions VALUES (?,?,?,?,?,?)", (recall_id, decision_id, query, outcome, int(missed), time.time()))
        return True

    def recall_audit(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM memory_recall_decisions ORDER BY created_at DESC")]

    def compose(self, result_memory_id: str, component_memory_ids: list[str], *, content: str, kind="compound", environment=None, source_event_ids=None, composition_bonus=0.0, redundancy_penalty=0.0, conflict_penalty=0.0):
        ids = list(component_memory_ids)
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("composition_components_must_be_unique")
        placeholders = ",".join("?" for _ in ids)
        rows = list(self.db.execute(f"SELECT memory_id, weight FROM memories WHERE memory_id IN ({placeholders})", ids))
        if len(rows) != len(ids):
            raise ValueError("composition_component_not_found")
        gain = composition_gain([float(row["weight"]) for row in rows], composition_bonus=composition_bonus, redundancy_penalty=redundancy_penalty, conflict_penalty=conflict_penalty)
        fingerprint = "|".join(sorted(ids))
        with self.db:
            if self.db.execute("SELECT 1 FROM memory_compositions WHERE component_fingerprint=?", (fingerprint,)).fetchone():
                raise ValueError("composition_already_exists")
            self.put(result_memory_id, kind, content, confidence=0.2, weight=min(5.0, gain), environment=environment, source_event_ids=source_event_ids, status="candidate")
            cid = f"cmp_{uuid.uuid4().hex}"
            self.db.execute("INSERT INTO memory_compositions VALUES (?,?,?,?,?,?,?,?,?)", (cid, result_memory_id, json.dumps(ids), fingerprint, composition_bonus, redundancy_penalty, conflict_penalty, 0, time.time()))
            self.db.execute("INSERT INTO memory_events VALUES (?,?,?,?,?)", (f"evt_{uuid.uuid4().hex}", result_memory_id, "memory_composed", json.dumps({"components": ids, "gain": gain}), time.time()))
        return {"composition_id": cid, "memory_id": result_memory_id, "gain": gain}

    def update_skill(self, domain: str, *, level: float, marginal: float, contradiction=False, novel_environment=False, independent_successes=0, composition_successes=0, regression_evidence=False):
        row = self.db.execute("SELECT * FROM skill_states WHERE skill_domain=?", (domain,)).fetchone()
        old = SkillState(SkillPhase(row["phase"]), row["level"], row["abstraction_level"], row["marginal_gain"], row["plateau_score"], row["activation_count"]) if row else SkillState()
        state = transition_phase(SkillState(old.phase, min(1.0, max(0.0, level)), old.abstraction_level, old.marginal_gain, old.plateau_score, old.activation_count), marginal=marginal, contradiction=contradiction, novel_environment=novel_environment, independent_successes=independent_successes, composition_successes=composition_successes, regression_evidence=regression_evidence)
        from contextlib import nullcontext
        with (nullcontext() if self.db.in_transaction else self.db):
            self.db.execute("INSERT OR REPLACE INTO skill_states VALUES (?,?,?,?,?,?,?,?)", (domain, state.phase.value, state.level, state.abstraction_level, state.marginal_gain, state.plateau_score, state.activation_count, time.time()))
            if state.phase != old.phase:
                event_type = "memory_phase_regressed" if list(SkillPhase).index(state.phase) < list(SkillPhase).index(old.phase) else "memory_phase_advanced"
                self.db.execute("INSERT INTO skill_events VALUES (?,?,?,?,?)", (f"sev_{uuid.uuid4().hex}", domain, event_type, json.dumps({"from": old.phase.value, "to": state.phase.value, "regression_evidence": regression_evidence}), time.time()))
        return state

    def assess_memory_degradation(self, memory_id: str, *, now: float | None = None, half_life_days: float = 30.0, current_environment=None, consecutive_failures: int = 0, contradiction_count: int = 0, dependency_drift_score: float = 0.0, hard_failure: bool = False):
        row = self.db.execute("SELECT environment, created_at FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        if not row:
            raise ValueError("memory_not_found")
        reference = json.loads(row["environment"] or "{}")
        current = current_environment or reference
        drift = environment_drift(reference, current)
        timestamp = time.time() if now is None else float(now)
        assessment = assess_degradation(age_days=max(0.0, (timestamp - row["created_at"]) / 86400.0), half_life_days=half_life_days, consecutive_failures=consecutive_failures, contradiction_count=contradiction_count, environment_drift_score=drift, dependency_drift_score=dependency_drift_score, hard_failure=hard_failure)
        with self.db:
            base = self.db.execute("SELECT base_weight, base_confidence, base_status FROM memory_degradation WHERE memory_id=?", (memory_id,)).fetchone()
            base_row = self.db.execute("SELECT weight, confidence, status FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            base_weight = float(base["base_weight"]) if base else float(base_row["weight"])
            base_confidence = float(base["base_confidence"]) if base else float(base_row["confidence"])
            base_status = str(base["base_status"]) if base else str(base_row["status"])
            status = "disputed" if assessment.state == "disputed" and base_status in {"warm", "hot"} else base_status
            self.db.execute("INSERT OR REPLACE INTO memory_degradation(memory_id,base_weight,base_confidence,base_status,score,freshness,state,reasons,model_version,assessed_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (memory_id, base_weight, base_confidence, base_status, assessment.score, assessment.freshness, assessment.state, json.dumps(assessment.reasons), assessment.model_version, timestamp))
            self.db.execute("UPDATE memories SET weight=?, confidence=?, status=?, updated_at=? WHERE memory_id=?", (base_weight * assessment.freshness, base_confidence * assessment.freshness, status, timestamp, memory_id))
            self.db.execute("INSERT INTO memory_maintenance VALUES (?,?,?,?,?)", (f"maint_{uuid.uuid4().hex}", memory_id, "degradation_assessed", json.dumps({"score": assessment.score, "state": assessment.state, "reasons": assessment.reasons}), timestamp))
            if hard_failure:
                self.db.execute("UPDATE memories SET status='invalidated' WHERE memory_id=?", (memory_id,))
                self.db.execute("UPDATE memory_versions SET version_status='invalidated' WHERE memory_id=?", (memory_id,))
        return assessment

    def cool(self, memory_id: str, *, reason="stale"):
        with self.db:
            changed = self.db.execute("UPDATE memories SET status='cold', updated_at=? WHERE memory_id=? AND status IN ('warm','hot','disputed','candidate','pending_validation')", (time.time(), memory_id)).rowcount
            if changed:
                self.db.execute("UPDATE memory_versions SET version_status='cold' WHERE memory_id=? AND version_status IN ('warm','hot','candidate')", (memory_id,))
                self.db.execute("INSERT INTO memory_maintenance VALUES (?,?,?,?,?)", (f"maint_{uuid.uuid4().hex}", memory_id, "cool", reason, time.time()))
        return changed == 1

    def rewarm(self, memory_id: str, *, success: bool, evidence_ref: str | None = None):
        if success and not evidence_ref:
            raise ValueError("evidence_required_for_rewarm")
        with self.db:
            row = self.db.execute("SELECT status, weight, confidence FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            if not row or row["status"] != "cold":
                raise ValueError("cold_memory_not_found")
            status = "warm" if success else "cold"
            weight = min(5.0, float(row["weight"]) + (0.25 if success else -0.1))
            confidence = min(1.0, float(row["confidence"]) + (0.05 if success else 0.0))
            self.db.execute("UPDATE memories SET status=?, weight=?, confidence=?, updated_at=? WHERE memory_id=?", (status, weight, confidence, time.time(), memory_id))
            if success:
                self.db.execute("DELETE FROM memory_degradation WHERE memory_id=?", (memory_id,))
                self.db.execute("UPDATE memory_versions SET version_status='warm' WHERE memory_id=? AND version=(SELECT MAX(version) FROM memory_versions WHERE memory_id=?)", (memory_id, memory_id))
            self.db.execute("INSERT INTO memory_maintenance VALUES (?,?,?,?,?)", (f"maint_{uuid.uuid4().hex}", memory_id, "rewarm", json.dumps({"success": success, "evidence_ref": evidence_ref}), time.time()))
        return status
