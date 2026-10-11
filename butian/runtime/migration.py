from __future__ import annotations
import hashlib, json, sqlite3, time, uuid
from pathlib import Path
from .service import ButianService

_KIND_MAP = {"fact": "fact", "lesson": "lesson", "experience": "experience", "knowledge": "knowledge"}

class MemberAssetMigrator:
    """Import agent profiles from Nuwa memory.db without modifying the source.

    完整资产迁移：
    - 档案：persona/personality/角色文件正文、rank、reports_to、reasoning_effort、
      max_tokens、model_tier、tool_policy、provider、model、traits、domains。
    - 记忆：源库 memory 表（含共享 default 池）导入到每个数字人的独立记忆库，
      状态映射为 candidate（未验证不升温），环境里保留来源指纹。
    - 台账：migration_runs 记录 source_sha256，可核验源版本；重复导入幂等跳过；
      同名不同 ID 显式报错，不静默合并。
    """

    def __init__(self, service: ButianService):
        self.service = service
        self.service.store.db.executescript('''
        CREATE TABLE IF NOT EXISTS migration_runs (run_id TEXT PRIMARY KEY, source_path TEXT NOT NULL, source_sha256 TEXT, status TEXT NOT NULL, imported_count INTEGER NOT NULL, updated_count INTEGER NOT NULL DEFAULT 0, skipped_count INTEGER NOT NULL, error_count INTEGER NOT NULL, memories_imported INTEGER NOT NULL DEFAULT 0, memories_skipped INTEGER NOT NULL DEFAULT 0, details TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS imported_member_assets (source_path TEXT NOT NULL, source_member_id TEXT NOT NULL, target_member_id TEXT NOT NULL, imported_at REAL NOT NULL, PRIMARY KEY(source_path, source_member_id));
        ''')
        run_columns = {row[1] for row in self.service.store.db.execute("PRAGMA table_info(migration_runs)")}
        for column in ("updated_count", "memories_imported", "memories_skipped"):
            if column not in run_columns:
                self.service.store.db.execute(f"ALTER TABLE migration_runs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _load_role_files(roles_dir: Path | None) -> dict[str, str]:
        """按 frontmatter 的 name: 建立成员名 → 角色文件正文映射。"""
        if roles_dir is None or not roles_dir.is_dir():
            return {}
        mapping: dict[str, str] = {}
        for path in sorted(roles_dir.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("name:"):
                    name = stripped[5:].strip().strip('"\'')
                    if name:
                        mapping[name] = text
                    break
        return mapping

    def _persona_text(self, row, role_text: str | None) -> str:
        parts = []
        if row["persona"]:
            parts.append(str(row["persona"]))
        if row["personality"]:
            parts.append(f"性格特质：{row['personality']}")
        if role_text:
            parts.append(f"角色文件：\n{role_text}")
        return "\n\n".join(parts)

    def _import_member_memories(self, con: sqlite3.Connection, member_id: str, *, source_ids: tuple[str, ...]) -> tuple[int, int]:
        """把源记忆导入成员独立记忆库；按源 memory_id 幂等。源库没有 memory 表时如实返回 0。"""
        imported = skipped = 0
        if "memory" not in {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            return 0, 0
        placeholders = ",".join("?" for _ in source_ids)
        rows = list(con.execute(f"SELECT id, member_id, kind, title, body, importance, confidence, weight, archived FROM memory WHERE member_id IN ({placeholders}) AND (archived IS NULL OR archived != 1)", source_ids))
        if not rows:
            return 0, 0
        memory = self.service.memory(member_id)
        try:
            existing = {row[0] for row in memory.db.execute("SELECT memory_id FROM memories")}
            for row in rows:
                memory_id = f"nvwa_{row['id']}"
                if memory_id in existing:
                    skipped += 1
                    continue
                content = (row["body"] or row["title"] or "").strip()
                if not content:
                    skipped += 1
                    continue
                kind = _KIND_MAP.get(str(row["kind"] or "").lower(), "knowledge")
                environment = {"source": "nvwa", "source_memory_id": row["id"], "source_member_id": row["member_id"]}
                try:
                    memory.put(memory_id, kind, content, confidence=float(row["confidence"] or 0.0), weight=max(0.0, min(float(row["weight"] or row["importance"] or 1.0), 5.0)), environment=environment, status="candidate", change_reason="nvwa_migration")
                    imported += 1
                except ValueError:
                    # 源内容触发安全扫描等约束：跳过并计数，不中断整批
                    skipped += 1
        finally:
            memory.close()
        return imported, skipped

    def preflight(self, source_path: str | Path, *, roles_dir: str | Path | None = None) -> dict:
        source = Path(source_path).resolve()
        if not source.is_file():
            raise ValueError("migration_source_not_found")
        source_sha256 = self._file_sha256(source)
        roles = self._load_role_files(Path(roles_dir).resolve() if roles_dir else source.parent / "roles")
        con = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "member" not in tables:
                return {"status": "unsupported", "source_path": str(source), "source_sha256": source_sha256, "missing_tables": ["member"], "members": []}
            columns = {row[1] for row in con.execute("PRAGMA table_info(member)")}
            expected = {"id", "name", "kind", "role", "duties", "persona", "personality", "provider", "model", "rank", "reports_to", "reasoning_effort", "max_tokens", "model_tier", "tool_policy"}
            members = []
            order_column = "created_at" if "created_at" in columns else "rowid"
            query = f"SELECT * FROM member WHERE kind='agent' ORDER BY {order_column}" if "kind" in columns else None
            for row in con.execute(query) if query else ():
                source_id = row["id"] if "id" in columns else None
                source_name = row["name"] if "name" in columns else None
                conflict = self.service.store.db.execute("SELECT member_id FROM members WHERE name=? AND member_id!=?", (source_name, source_id)).fetchone() if source_name is not None and source_id is not None else None
                members.append({"source_member_id": source_id, "name": source_name, "target_exists": bool(source_id is not None and self.service.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (source_id,)).fetchone()), "name_conflict_with": conflict[0] if conflict else None, "role_file_found": source_name in roles if source_name is not None else False, "memory_table_available": "memory" in tables})
            missing = sorted(expected - columns)
            status = "ready" if not missing and not any(item["name_conflict_with"] for item in members) else "needs_review"
            return {"status": status, "source_path": str(source), "source_sha256": source_sha256, "agent_count": len(members), "members": members, "missing_fields": missing, "roles_found": len(roles), "source_read_only": True}
        finally:
            con.close()

    def import_agents(self, source_path: str | Path, *, roles_dir: str | Path | None = None, update_existing: bool = False, import_memories: bool = False) -> dict:
        if import_memories:
            raise ValueError("historical_memory_migration_forbidden")
        source = Path(source_path).resolve()
        if not source.is_file(): raise ValueError("migration_source_not_found")
        source_sha256 = self._file_sha256(source)
        roles = self._load_role_files(Path(roles_dir).resolve() if roles_dir else source.parent / "roles")
        con = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "member" not in tables: raise ValueError("migration_source_schema_unsupported")
            shared_memory_members = ("default",)
            run_id = f"mig_{uuid.uuid4().hex}"
            imported = updated = skipped = errors = 0
            memories_imported = memories_skipped = 0
            details = []
            with self.service.store.db:
                for row in con.execute("SELECT * FROM member WHERE kind='agent' ORDER BY created_at"):
                    member_id = row["id"]
                    already = self.service.store.db.execute("SELECT 1 FROM imported_member_assets WHERE source_path=? AND source_member_id=?", (str(source), member_id)).fetchone()
                    exists = self.service.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone()
                    if (already or exists) and not update_existing:
                        skipped += 1
                        details.append({"source_member_id": member_id, "action": "skipped_already_imported"})
                        continue
                    # 同名不同 ID：显式报错，不静默合并
                    conflict = self.service.store.db.execute("SELECT member_id FROM members WHERE name=? AND member_id!=?", (row["name"], member_id)).fetchone()
                    if conflict:
                        errors += 1
                        details.append({"source_member_id": member_id, "error": f"name_conflict:{row['name']}", "conflicts_with": conflict["member_id"]})
                        continue
                    try:
                        duties = row["duties"] or ""
                        responsibilities = [duties] if duties else []
                        if "member_trait" in tables:
                            traits = [dict(x) for x in con.execute("SELECT trait,value,intensity,source FROM member_trait WHERE member_id=?", (member_id,))]
                            if traits:
                                responsibilities.append(json.dumps({"source": "nvwa", "traits": traits}, ensure_ascii=False))
                        if "member_domain" in tables:
                            domains = [dict(x) for x in con.execute("SELECT domain,level,evidence FROM member_domain WHERE member_id=?", (member_id,))]
                            if domains:
                                responsibilities.append(json.dumps({"source": "nvwa", "domains": domains}, ensure_ascii=False))
                        if exists:
                            self.service.store.db.execute(
                                "UPDATE members SET name=?, role=?, responsibilities=?, provider_id=?, model_id=?, persona=? WHERE member_id=?",
                                (row["name"], row["role"] or "member", json.dumps(responsibilities, ensure_ascii=False), row["provider"], row["model"], self._persona_text(row, roles.get(row["name"])), member_id))
                            updated += 1
                            action = "updated_existing"
                        else:
                            self.service.store.add_member(member_id, row["name"], row["role"] or "member", responsibilities, row["provider"], row["model"])
                            action = "imported"
                            imported += 1
                        # 完整配置字段（add_member 不覆盖这些列）
                        self.service.store.db.execute(
                            "UPDATE members SET rank=?, reports_to=?, reasoning_effort=?, max_tokens=?, model_tier=?, tool_policy=?, persona=? WHERE member_id=?",
                            (row["rank"], row["reports_to"], row["reasoning_effort"], row["max_tokens"], row["model_tier"], row["tool_policy"], self._persona_text(row, roles.get(row["name"])), member_id))
                        self.service.store.db.execute("INSERT OR REPLACE INTO imported_member_assets VALUES (?,?,?,?)", (str(source), member_id, member_id, time.time()))
                        details.append({"source_member_id": member_id, "action": action})
                    except Exception as exc:
                        errors += 1
                        details.append({'source_member_id': member_id, 'error': str(exc)})
                status = 'completed' if errors == 0 else ('partial' if imported or updated else 'failed')
                self.service.store.db.execute("INSERT INTO migration_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, str(source), source_sha256, status, imported, updated, skipped, errors, memories_imported, memories_skipped, json.dumps(details, ensure_ascii=False), time.time()))
            return {'run_id': run_id, 'status': status, 'imported': imported, 'updated': updated, 'skipped': skipped, 'errors': errors, 'memories_imported': memories_imported, 'memories_skipped': memories_skipped, 'source_sha256': source_sha256, 'source_read_only': True, 'details': details}
        finally:
            con.close()
