from __future__ import annotations
import json, time, uuid
from pathlib import Path
from .memory import MemoryStore

class DreamRunner:
    def __init__(self, memory: MemoryStore):
        self.memory = memory
        self.memory.db.executescript('''
        CREATE TABLE IF NOT EXISTS dream_runs (run_id TEXT PRIMARY KEY, started_at REAL NOT NULL, status TEXT NOT NULL, gate_snapshot TEXT NOT NULL, candidate_count INTEGER NOT NULL DEFAULT 0, reason TEXT);
        CREATE TABLE IF NOT EXISTS dream_candidates (candidate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, memory_ids TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS dream_lock (lock_id INTEGER PRIMARY KEY CHECK(lock_id=1), owner TEXT NOT NULL, acquired_at REAL NOT NULL);
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
