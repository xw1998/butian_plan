from __future__ import annotations
import json, sqlite3, time, uuid
from pathlib import Path
from .service import ButianService

class MemberAssetMigrator:
    """Import agent profiles from Nuwa memory.db without modifying the source."""
    def __init__(self, service: ButianService):
        self.service = service
        self.service.store.db.executescript('''
        CREATE TABLE IF NOT EXISTS migration_runs (run_id TEXT PRIMARY KEY, source_path TEXT NOT NULL, source_sha256 TEXT, status TEXT NOT NULL, imported_count INTEGER NOT NULL, skipped_count INTEGER NOT NULL, error_count INTEGER NOT NULL, details TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS imported_member_assets (source_path TEXT NOT NULL, source_member_id TEXT NOT NULL, target_member_id TEXT NOT NULL, imported_at REAL NOT NULL, PRIMARY KEY(source_path, source_member_id));
        ''')

    def import_agents(self, source_path: str | Path) -> dict:
        source = Path(source_path).resolve()
        if not source.is_file(): raise ValueError("migration_source_not_found")
        con = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"member", "member_trait", "member_domain"}
            if not required <= tables: raise ValueError("migration_source_schema_unsupported")
            run_id = f"mig_{uuid.uuid4().hex}"
            imported = skipped = errors = 0
            details = []
            with self.service.store.db:
                for row in con.execute("SELECT * FROM member WHERE kind='agent' ORDER BY created_at"):
                    existing = self.service.store.db.execute("SELECT 1 FROM imported_member_assets WHERE source_path=? AND source_member_id=?", (str(source), row['id'])).fetchone()
                    if existing or self.service.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (row['id'],)).fetchone():
                        skipped += 1; continue
                    traits = [dict(x) for x in con.execute("SELECT trait,value,intensity,source FROM member_trait WHERE member_id=?", (row['id'],))]
                    domains = [dict(x) for x in con.execute("SELECT domain,level,evidence FROM member_domain WHERE member_id=?", (row['id'],))]
                    responsibilities = json.loads(row['duties'] and json.dumps([row['duties']]) or '[]')
                    responsibilities.append(json.dumps({'source': 'nvwa', 'traits': traits, 'domains': domains}, ensure_ascii=False))
                    try:
                        self.service.store.add_member(row['id'], row['name'], row['role'] or 'member', responsibilities, row['provider'], row['model'])
                        self.service.store.db.execute("INSERT INTO imported_member_assets VALUES (?,?,?,?)", (str(source), row['id'], row['id'], time.time()))
                        imported += 1
                    except Exception as exc:
                        errors += 1; details.append({'source_member_id': row['id'], 'error': str(exc)})
                status = 'completed' if errors == 0 else ('partial' if imported else 'failed')
                self.service.store.db.execute("INSERT INTO migration_runs VALUES (?,?,?,?,?,?,?,?,?)", (run_id, str(source), None, status, imported, skipped, errors, json.dumps(details, ensure_ascii=False), time.time()))
            return {'run_id': run_id, 'status': status, 'imported': imported, 'skipped': skipped, 'errors': errors, 'source_read_only': True, 'details': details}
        finally:
            con.close()
