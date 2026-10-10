from __future__ import annotations
import json
import sqlite3
import time
import uuid
from pathlib import Path
from .security import assert_safe_memory_content
from ..core.experience import SkillPhase, SkillState, composition_gain, decision_value, transition_phase

SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS memories (memory_id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL, confidence REAL NOT NULL, weight REAL NOT NULL, environment TEXT NOT NULL, source_event_ids TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, content);
CREATE TABLE IF NOT EXISTS memory_events (event_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind, status);
CREATE TABLE IF NOT EXISTS memory_versions (version_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, version INTEGER NOT NULL, parent_version_id TEXT, content TEXT NOT NULL, environment TEXT NOT NULL, change_reason TEXT, created_at REAL NOT NULL, UNIQUE(memory_id, version));
CREATE TABLE IF NOT EXISTS memory_conflicts (conflict_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, conflicting_memory_id TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_recall_log (recall_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, query TEXT NOT NULL, layer TEXT NOT NULL, used INTEGER NOT NULL, score REAL NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_maintenance (maintenance_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_compositions (composition_id TEXT PRIMARY KEY, result_memory_id TEXT NOT NULL, component_memory_ids TEXT NOT NULL, component_fingerprint TEXT NOT NULL UNIQUE, composition_bonus REAL NOT NULL, redundancy_penalty REAL NOT NULL, conflict_penalty REAL NOT NULL, observed_successes INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS skill_states (skill_domain TEXT PRIMARY KEY, phase TEXT NOT NULL, level REAL NOT NULL, abstraction_level INTEGER NOT NULL, marginal_gain REAL NOT NULL, plateau_score REAL NOT NULL, activation_count INTEGER NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS skill_events (event_id TEXT PRIMARY KEY, skill_domain TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
'''

class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def put(self, memory_id: str, kind: str, content: str, *, confidence=0.0, weight=1.0, environment=None, source_event_ids=None, status="candidate", source_validator=None, parent_version_id=None, change_reason=None):
        assert_safe_memory_content(memory_id, kind, content, json.dumps(environment or {}, ensure_ascii=False))
        if status in {"warm", "hot"} and not source_event_ids:
            raise ValueError("evidence_required_for_verified_memory")
        if source_event_ids and source_validator and not source_validator(list(source_event_ids)):
            raise ValueError("invalid_memory_source_events")
        now = time.time()
        with self.db:
            previous = self.db.execute("SELECT memory_id, content FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            self.db.execute("INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)", (memory_id, kind, content, status, confidence, weight, json.dumps(environment or {}), json.dumps(source_event_ids or []), now, now))
            self.db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
            self.db.execute("INSERT INTO memory_fts(memory_id,content) VALUES (?,?)", (memory_id, content))
            version = self.db.execute("SELECT COALESCE(MAX(version),0)+1 FROM memory_versions WHERE memory_id=?", (memory_id,)).fetchone()[0]
            self.db.execute("INSERT INTO memory_versions VALUES (?,?,?,?,?,?,?,?)", (f"ver_{uuid.uuid4().hex}", memory_id, version, parent_version_id, content, json.dumps(environment or {}, ensure_ascii=False), change_reason, now))
            if previous and previous["content"] != content:
                conflict = self.db.execute("SELECT 1 FROM memories WHERE memory_id!=? AND content=? AND status NOT IN ('invalidated','archived')", (memory_id, content)).fetchone()
                if conflict:
                    self.db.execute("INSERT INTO memory_conflicts VALUES (?,?,?,?,?,?)", (f"conf_{uuid.uuid4().hex}", memory_id, conflict["memory_id"], "same_content_different_memory", "open", now))

    def search(self, query: str, *, kind=None, limit=20, environment=None, include_cold=True, used=False):
        """Layered recall: hot/warm first, candidate/cold as low-confidence references."""
        sql = "SELECT m.* FROM memory_fts f JOIN memories m ON m.memory_id=f.memory_id WHERE f.content MATCH ? AND m.status NOT IN ('invalidated','archived')"
        args: list[object] = [query]
        if kind:
            sql += " AND m.kind=?"; args.append(kind)
        if not include_cold:
            sql += " AND m.status!='cold'"
        sql += " ORDER BY CASE m.status WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 WHEN 'candidate' THEN 2 WHEN 'disputed' THEN 3 ELSE 4 END, m.weight DESC, m.updated_at DESC LIMIT ?"; args.append(limit)
        rows = list(self.db.execute(sql, args))
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
                self.db.execute("INSERT INTO memory_recall_log VALUES (?,?,?,?,?,?,?)", (f"rec_{uuid.uuid4().hex}", item["memory_id"], query, layer, int(used), float(item["weight"]), now))
        return result

    def compose(self, result_memory_id: str, component_memory_ids: list[str], *, content: str, kind="compound", environment=None, source_event_ids=None, composition_bonus=0.0, redundancy_penalty=0.0, conflict_penalty=0.0):
        """Create one composite memory; duplicate component ids are rejected before scoring."""
        ids = list(component_memory_ids)
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("composition_components_must_be_unique")
        placeholders = ",".join("?" for _ in ids)
        rows = list(self.db.execute(f"SELECT memory_id, weight FROM memories WHERE memory_id IN ({placeholders})", ids))
        if len(rows) != len(ids): raise ValueError("composition_component_not_found")
        gain = composition_gain([float(row["weight"]) for row in rows], composition_bonus=composition_bonus, redundancy_penalty=redundancy_penalty, conflict_penalty=conflict_penalty)
        fingerprint = "|".join(sorted(ids))
        with self.db:
            existing = self.db.execute("SELECT composition_id FROM memory_compositions WHERE component_fingerprint=?", (fingerprint,)).fetchone()
            if existing: raise ValueError("composition_already_exists")
            self.put(result_memory_id, kind, content, confidence=0.2, weight=min(5.0, gain), environment=environment, source_event_ids=source_event_ids, status="candidate")
            cid = f"cmp_{uuid.uuid4().hex}"
            self.db.execute("INSERT INTO memory_compositions VALUES (?,?,?,?,?,?,?,?,?)", (cid, result_memory_id, json.dumps(ids), fingerprint, composition_bonus, redundancy_penalty, conflict_penalty, 0, time.time()))
            self.db.execute("INSERT INTO memory_events VALUES (?,?,?,?,?)", (f"evt_{uuid.uuid4().hex}", result_memory_id, "memory_composed", json.dumps({"components": ids, "gain": gain}), time.time()))
        return {"composition_id": cid, "memory_id": result_memory_id, "gain": gain}

    def update_skill(self, domain: str, *, level: float, marginal: float, contradiction=False, novel_environment=False, independent_successes=0, composition_successes=0):
        row = self.db.execute("SELECT * FROM skill_states WHERE skill_domain=?", (domain,)).fetchone()
        old = SkillState(SkillPhase(row["phase"]), row["level"], row["abstraction_level"], row["marginal_gain"], row["plateau_score"], row["activation_count"]) if row else SkillState()
        state = transition_phase(SkillState(old.phase, min(1.0, max(0.0, level)), old.abstraction_level, old.marginal_gain, old.plateau_score, old.activation_count), marginal=marginal, contradiction=contradiction, novel_environment=novel_environment, independent_successes=independent_successes, composition_successes=composition_successes)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO skill_states VALUES (?,?,?,?,?,?,?,?)", (domain, state.phase.value, state.level, state.abstraction_level, state.marginal_gain, state.plateau_score, state.activation_count, time.time()))
            if state.phase != old.phase:
                self.db.execute("INSERT INTO skill_events VALUES (?,?,?,?,?)", (f"sev_{uuid.uuid4().hex}", domain, "memory_phase_advanced", json.dumps({"from": old.phase.value, "to": state.phase.value}), time.time()))
        return state

    def cool(self, memory_id: str, *, reason="stale"):
        with self.db:
            changed = self.db.execute("UPDATE memories SET status='cold', updated_at=? WHERE memory_id=? AND status IN ('warm','hot')", (time.time(), memory_id)).rowcount
            if changed:
                self.db.execute("INSERT INTO memory_maintenance VALUES (?,?,?,?,?)", (f"maint_{uuid.uuid4().hex}", memory_id, "cool", reason, time.time()))
        return changed == 1

    def rewarm(self, memory_id: str, *, success: bool, evidence_ref: str | None = None):
        with self.db:
            row = self.db.execute("SELECT status, weight, confidence FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            if not row or row["status"] != "cold": raise ValueError("cold_memory_not_found")
            status = "warm" if success else "cold"
            weight = min(5.0, float(row["weight"]) + (0.25 if success else -0.1))
            confidence = min(1.0, float(row["confidence"]) + (0.05 if success else 0.0))
            self.db.execute("UPDATE memories SET status=?, weight=?, confidence=?, updated_at=? WHERE memory_id=?", (status, weight, confidence, time.time(), memory_id))
            self.db.execute("INSERT INTO memory_maintenance VALUES (?,?,?,?,?)", (f"maint_{uuid.uuid4().hex}", memory_id, "rewarm", json.dumps({"success": success, "evidence_ref": evidence_ref}), time.time()))
        return status
