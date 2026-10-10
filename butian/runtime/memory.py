from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS memories (memory_id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL, confidence REAL NOT NULL, weight REAL NOT NULL, environment TEXT NOT NULL, source_event_ids TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, content);
CREATE TABLE IF NOT EXISTS memory_events (event_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind, status);
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

    def put(self, memory_id: str, kind: str, content: str, *, confidence=0.0, weight=1.0, environment=None, source_event_ids=None, status="candidate"):
        if status in {"warm", "hot"} and not source_event_ids:
            raise ValueError("evidence_required_for_verified_memory")
        now = time.time()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)", (memory_id, kind, content, status, confidence, weight, json.dumps(environment or {}), json.dumps(source_event_ids or []), now, now))
            self.db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
            self.db.execute("INSERT INTO memory_fts(memory_id,content) VALUES (?,?)", (memory_id, content))

    def search(self, query: str, *, kind=None, limit=20):
        sql = "SELECT m.* FROM memory_fts f JOIN memories m ON m.memory_id=f.memory_id WHERE f.content MATCH ? AND m.status NOT IN ('invalidated','archived')"
        args: list[object] = [query]
        if kind:
            sql += " AND m.kind=?"; args.append(kind)
        sql += " ORDER BY m.weight DESC, m.updated_at DESC LIMIT ?"; args.append(limit)
        rows = list(self.db.execute(sql, args))
        if not rows:
            fallback = "SELECT * FROM memories WHERE content LIKE ? AND status NOT IN ('invalidated','archived')"
            fallback_args: list[object] = [f"%{query}%"]
            if kind:
                fallback += " AND kind=?"; fallback_args.append(kind)
            fallback += " ORDER BY weight DESC, updated_at DESC LIMIT ?"; fallback_args.append(limit)
            rows = list(self.db.execute(fallback, fallback_args))
        return [dict(row) for row in rows]
