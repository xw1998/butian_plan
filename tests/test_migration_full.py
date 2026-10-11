from __future__ import annotations
import pytest

import io
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from butian.runtime.service import ButianService


def _make_full_source(path: Path, roles_dir: Path | None = None):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE member (id TEXT PRIMARY KEY, kind TEXT, name TEXT, rank TEXT, role TEXT,
            duties TEXT, reports_to TEXT, team_id TEXT, provider TEXT, model TEXT,
            reasoning_effort TEXT, max_tokens INTEGER, model_tier TEXT, tool_policy TEXT,
            persona TEXT, personality TEXT, domains TEXT, status TEXT, created_at REAL, retired_at TEXT);
        CREATE TABLE member_trait (member_id TEXT, trait TEXT, value TEXT, intensity REAL, source TEXT);
        CREATE TABLE member_domain (member_id TEXT, domain TEXT, level REAL, evidence TEXT);
        CREATE TABLE memory (id TEXT PRIMARY KEY, member_id TEXT, kind TEXT, title TEXT, body TEXT,
            importance REAL, confidence REAL, weight REAL, archived INTEGER, created_at REAL, updated_at REAL);
        """
    )
    con.execute("INSERT INTO member VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("a1", "agent", "许伟", "资深", "嵌入式系统", "方案移植", None, None, "deepseek-account", "deepseek-flash",
                 "high", 8192, "strong", "allow_read_only", "人格正文一", "严谨", None, None, 1.0, None))
    con.execute("INSERT INTO member VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("a2", "agent", "沈衡", "资深", "仪器信号处理", "测量可信度", None, None, "deepseek-account", "deepseek-flash",
                 None, None, "strong", None, "人格正文二", None, None, None, 2.0, None))
    con.execute("INSERT INTO memory VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("smem1", "default", "lesson", "教训标题", "教训正文：先查资料再动手", 1.0, 0.8, 1.5, 0, 1.0, 1.0))
    con.execute("INSERT INTO memory VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("smem2", "a1", "fact", "事实标题", "许伟的专属事实：F427 板卡实测", 1.0, 0.9, 2.0, 0, 1.0, 1.0))
    con.execute("INSERT INTO memory VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("smem3", "default", "fact", "归档标题", "已归档不应迁移", 1.0, 0.9, 1.0, 1, 1.0, 1.0))
    con.commit()
    con.close()
    if roles_dir is not None:
        roles_dir.mkdir(parents=True, exist_ok=True)
        (roles_dir / "xu-wei.md").write_text("---\nname: 许伟\nrank: 资深\n---\n许伟角色文件正文", encoding="utf-8")


class FullMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.source = self.root / "memory.db"
        self.roles_dir = self.root / "roles"
        _make_full_source(self.source, self.roles_dir)
        self.before = self.source.read_bytes()

    def tearDown(self):
        self._tmp.cleanup()

    def test_full_asset_import(self):
        service = ButianService(self.root / "home")
        try:
            result = service.import_members(str(self.source), roles_dir=self.roles_dir, import_memories=False)
            self.assertEqual(result["errors"], 0)
            self.assertEqual(result["imported"], 2)
            row = service.store.db.execute("SELECT * FROM members WHERE member_id='a1'").fetchone()
            self.assertIn("人格正文一", row["persona"])
            self.assertIn("许伟角色文件正文", row["persona"])
            self.assertEqual(row["rank"], "资深")
            self.assertEqual(row["model_tier"], "strong")
            self.assertEqual(row["tool_policy"], "allow_read_only")
            self.assertEqual(int(row["max_tokens"]), 8192)
            self.assertEqual(row["reasoning_effort"], "high")
            # 历史记忆明确不迁移，成员独立库保持空白。
            mem1 = service.memory("a1")
            self.assertEqual(mem1.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)
            mem1.close()
        finally:
            service.close()
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_source_sha256_recorded(self):
        import hashlib
        service = ButianService(self.root / "home")
        try:
            service.import_members(str(self.source), roles_dir=self.roles_dir, import_memories=False)
            runs = service.list_migration_runs()
            self.assertEqual(runs[0]["source_sha256"], hashlib.sha256(self.before).hexdigest())
        finally:
            service.close()

    def test_name_conflict_reported(self):
        service = ButianService(self.root / "home")
        try:
            service.import_members(str(self.source), roles_dir=self.roles_dir, import_memories=False)
            # 用另一个源库制造同名不同 ID
            other = self.root / "other.db"
            con = sqlite3.connect(other)
            con.executescript(
                """
                CREATE TABLE member (id TEXT PRIMARY KEY, kind TEXT, name TEXT, rank TEXT, role TEXT,
                    duties TEXT, reports_to TEXT, team_id TEXT, provider TEXT, model TEXT,
                    reasoning_effort TEXT, max_tokens INTEGER, model_tier TEXT, tool_policy TEXT,
                    persona TEXT, personality TEXT, domains TEXT, status TEXT, created_at REAL, retired_at TEXT);
                """
            )
            con.execute("INSERT INTO member VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("b1", "agent", "许伟", None, "冲突角色", None, None, None, None, None, None, None, None, None, None, None, None, None, 1.0, None))
            con.commit()
            con.close()
            result = service.import_members(str(other))
            self.assertEqual(result["imported"], 0)
            self.assertEqual(result["errors"], 1)
            self.assertIn("name_conflict", result["details"][0]["error"])
        finally:
            service.close()

    def test_update_existing_completes_old_partial_import(self):
        service = ButianService(self.root / "home")
        try:
            # 模拟旧版只导入了部分档案
            service.store.add_member("a1", "许伟", "嵌入式系统", ["旧职责"], "deepseek-account", "deepseek-flash")
            from butian.runtime.migration import MemberAssetMigrator
            MemberAssetMigrator(service)  # 幂等建台账表
            service.store.db.execute("INSERT INTO imported_member_assets VALUES (?,?,?,?)", (str(self.source.resolve()), "a1", "a1", 1.0))
            result = service.import_members(str(self.source), roles_dir=self.roles_dir, update_existing=True, import_memories=False)
            self.assertEqual(result["updated"], 1)
            self.assertEqual(result["imported"], 1)
            row = service.store.db.execute("SELECT persona, rank FROM members WHERE member_id='a1'").fetchone()
            self.assertIn("许伟角色文件正文", row["persona"])
            self.assertEqual(row["rank"], "资深")
            mem = service.memory("a1")
            count = mem.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            mem.close()
        finally:
            service.close()

    def test_memory_idempotent_on_rerun(self):
        service = ButianService(self.root / "home")
        try:
            service.import_members(str(self.source), roles_dir=self.roles_dir, import_memories=False)
            result = service.import_members(str(self.source), roles_dir=self.roles_dir, update_existing=True, import_memories=False)
            self.assertEqual(result["memories_imported"], 0)
        finally:
            service.close()


def json_loads(text):
    import json
    return json.loads(text)


if __name__ == "__main__":
    unittest.main()


def test_migration_entrypoints_pass_params(tmp_path=None):
    """三个入口（HTTP/MCP/CLI）都必须把 roles_dir/update_existing/import_memories 透传给 import_members。"""
    import json as _json
    import argparse as _argparse
    import importlib as _importlib
    from butian.runtime.service import ButianService
    from butian.runtime import http_api as _http
    from butian import mcp_server as _mcp, cli as _cli

    home = Path(tempfile.mkdtemp(prefix="butian_entry_"))
    try:
        svc = ButianService(home)
        captured = {}
        def _fake_import(source_path, *, roles_dir=None, update_existing=False, import_memories=False):
            captured["args"] = (source_path, roles_dir, update_existing, import_memories)
            return {"status": "completed", "imported": 0}
        svc.import_members = _fake_import

        # 1) MCP 管理入口在缺少管理员身份时必须 fail-closed。
        with pytest.raises(ValueError, match="admin_authorization_required"):
            _mcp.call_tool(svc, "butian_member_import", {"source_path": "x.db"})

        # 2) CLI：全参数（类级 patch，CLI 自建 service 实例）
        def _fake_import_cls(_self, source_path, *, roles_dir=None, update_existing=False, import_memories=False):
            return _fake_import(source_path, roles_dir=roles_dir, update_existing=update_existing, import_memories=import_memories)
        orig = ButianService.import_members
        ButianService.import_members = _fake_import_cls
        try:
            argv = ["migrate", "import", "x.db", "--roles-dir", "r", "--update-existing", "--no-memories"]
            rc = _cli.main(argv, home_dir=home)
            assert rc == 0
        finally:
            ButianService.import_members = orig
        assert captured["args"] == ("x.db", "r", True, False), captured

        # 3) CLI preflight：source path 与 roles dir 透传
        preflight_args = {}
        def _fake_preflight(source_path, *, roles_dir=None):
            preflight_args["args"] = (source_path, roles_dir)
            return {"status": "ready"}
        orig_preflight = ButianService.preflight_members
        def _fake_preflight_cls(_self, source_path, *, roles_dir=None):
            return _fake_preflight(source_path, roles_dir=roles_dir)
        ButianService.preflight_members = _fake_preflight_cls
        try:
            rc = _cli.main(["migrate", "preflight", "x.db", "--roles-dir", "r"], home_dir=home)
            assert rc == 0
        finally:
            ButianService.preflight_members = orig_preflight
        assert preflight_args["args"] == ("x.db", "r"), preflight_args

        svc.preflight_members = _fake_preflight
        # MCP 管理入口无管理员身份时拒绝。
        with pytest.raises(ValueError, match="admin_authorization_required"):
            _mcp.call_tool(svc, "butian_member_preflight", {"source_path": "x.db", "roles_dir": "r"})

        # 6) HTTP preflight
        handler = _http.ApiHandler.__new__(_http.ApiHandler)
        handler.service = svc
        raw = _json.dumps({"source_path": "x.db", "roles_dir": "r"}).encode()
        handler.path = "/api/migration/preflight"
        handler.rfile = io.BytesIO(raw)
        handler.headers = {"Content-Length": str(len(raw))}
        sent_preflight = {}
        handler._send = lambda code, data=None: sent_preflight.update({"code": code, "data": data})
        handler.do_POST()
        assert preflight_args["args"] == ("x.db", "r"), preflight_args
        assert sent_preflight["code"] == 200, sent_preflight

        # 7) HTTP import：全参数（直接构造 handler 走 do_POST，_send 截获）
        handler = _http.ApiHandler.__new__(_http.ApiHandler)
        handler.service = svc
        raw = _json.dumps({"source_path": "x.db", "roles_dir": "r", "update_existing": True, "import_memories": False}).encode()
        handler.path = "/api/migration/import"
        handler.rfile = io.BytesIO(raw)
        handler.headers = {"Content-Length": str(len(raw))}
        sent = {}
        handler._send = lambda code, data=None: sent.update({"code": code, "data": data})
        handler.do_POST()
        assert captured["args"] == ("x.db", "r", True, False), captured
        assert sent["code"] == 201, sent
        print("test_migration_entrypoints_pass_params: OK")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_migration_preflight_reports_schema_and_conflicts_without_writing(tmp_path=None):
    import hashlib
    import sqlite3
    import tempfile
    from pathlib import Path
    from butian.runtime.service import ButianService

    root = Path(tempfile.mkdtemp(prefix="butian_preflight_"))
    source = root / "source.db"
    try:
        con = sqlite3.connect(source)
        con.executescript("CREATE TABLE member (id TEXT, kind TEXT, name TEXT, created_at REAL); INSERT INTO member VALUES ('src1','agent','Same',1); INSERT INTO member VALUES ('src2','agent','New',2);")
        con.commit()
        con.close()
        before = source.read_bytes()
        service = ButianService(root / "home")
        try:
            service.store.add_member("target", "Same")
            result = service.preflight_members(source)
            assert result["status"] == "needs_review", result
            assert result["agent_count"] == 2, result
            assert result["missing_fields"] and "persona" in result["missing_fields"], result
            assert result["members"][0]["name_conflict_with"] == "target", result
            assert result["source_sha256"] == hashlib.sha256(before).hexdigest(), result
            assert result["source_read_only"] is True
            assert source.read_bytes() == before
        finally:
            service.close()
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_migration_preflight_handles_missing_order_and_identity_columns(tmp_path=None):
    import sqlite3
    import tempfile
    from pathlib import Path
    from butian.runtime.service import ButianService

    root = Path(tempfile.mkdtemp(prefix="butian_preflight_schema_"))
    source = root / "source.db"
    try:
        con = sqlite3.connect(source)
        con.executescript("CREATE TABLE member (kind TEXT, role TEXT); INSERT INTO member VALUES ('agent','analyst');")
        con.commit()
        con.close()
        service = ButianService(root / "home")
        try:
            result = service.preflight_members(source)
            assert result["status"] == "needs_review", result
            assert result["members"] == [{"source_member_id": None, "name": None, "target_exists": False, "name_conflict_with": None, "role_file_found": False, "memory_table_available": False}], result
            assert "id" in result["missing_fields"] and "name" in result["missing_fields"], result
        finally:
            service.close()
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
