"""女娲数字人迁移到补天的定向测试（标准库可独立运行）。

覆盖：只读导入 agent 成员、过滤 human、源库不被修改、重复导入 skipped、
迁移台账可查询，以及 HTTP / MCP 迁移入口的连通性。
"""
from __future__ import annotations
import pytest

import json
import sqlite3
import tempfile
import threading
from pathlib import Path
from urllib import request as urlrequest

from butian.runtime.service import ButianService

def _make_source(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE member (id TEXT PRIMARY KEY, kind TEXT, name TEXT, rank TEXT, role TEXT,
            duties TEXT, reports_to TEXT, team_id TEXT, provider TEXT, model TEXT,
            reasoning_effort TEXT, max_tokens INTEGER, model_tier TEXT, tool_policy TEXT,
            persona TEXT, personality TEXT, domains TEXT, status TEXT, created_at REAL, retired_at REAL);
        CREATE TABLE member_trait (member_id TEXT, trait TEXT, value TEXT, intensity REAL, source TEXT);
        CREATE TABLE member_domain (member_id TEXT, domain TEXT, level REAL, evidence TEXT);
        """
    )
    con.execute("INSERT INTO member VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("u1", "human", "用户", None, "项目负责人", None, None, None, None, None, None, None, None, None, None, None, None, None, 1.0, None))
    con.execute("INSERT INTO member VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("a1", "agent", "许伟", None, "嵌入式系统", "方案选型与移植", None, None, "deepseek-account", "deepseek-flash", None, None, None, None, None, None, None, None, 2.0, None))
    con.execute("INSERT INTO member VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("a2", "agent", "沈衡", None, "仪器信号处理", "测量可信度优先", None, None, "deepseek-account", "deepseek-flash", None, None, None, None, None, None, None, None, 3.0, None))
    con.execute("INSERT INTO member_trait VALUES (?,?,?,?,?)", ("a1", "不猜", None, 0.95, "user"))
    con.execute("INSERT INTO member_domain VALUES (?,?,?,?)", ("a1", "嵌入式系统", 0.92, "角色文件"))
    con.commit()
    con.close()

def test_member_import_read_only_and_idempotent(tmp_path):
    source = tmp_path / "memory.db"
    _make_source(source)
    before = source.read_bytes()

    service = ButianService(tmp_path / "home")
    try:
        result = service.import_members(str(source))
        assert result["imported"] == 2 and result["skipped"] == 0 and result["errors"] == 0
        members = {m["member_id"]: m for m in service.store.db.execute("SELECT * FROM members")}
        assert set(members) == {"a1", "a2"}
        assert members["a1"]["name"] == "许伟" and members["a1"]["provider_id"] == "deepseek-account"
        assert "不猜" in members["a1"]["responsibilities"]

        again = service.import_members(str(source))
        assert again["imported"] == 0 and again["skipped"] == 2

        runs = service.list_migration_runs()
        assert len(runs) == 2 and runs[0]["source_path"] == str(source.resolve())
    finally:
        service.close()

    assert source.read_bytes() == before, "源库必须保持只读不变"

def test_http_migration_endpoint(tmp_path):
    from butian.runtime.http_api import serve

    source = tmp_path / "memory.db"
    _make_source(source)
    home = tmp_path / "home"
    thread = threading.Thread(target=serve, kwargs={"home": home, "host": "127.0.0.1", "port": 8799}, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            urlrequest.urlopen("http://127.0.0.1:8799/api/status", timeout=1).read()
            break
        except Exception:
            threading.Event().wait(0.1)
    payload = json.dumps({"source_path": str(source)}).encode()
    req = urlrequest.Request("http://127.0.0.1:8799/api/migration/import", data=payload, headers={"Content-Type": "application/json"})
    body = json.loads(urlrequest.urlopen(req, timeout=3).read())
    assert body["status"] == "ok" and body["data"]["imported"] == 2
    runs = json.loads(urlrequest.urlopen("http://127.0.0.1:8799/api/migration/runs", timeout=3).read())
    assert len(runs["data"]["runs"]) == 1

def test_mcp_migration_tool(tmp_path):
    from butian.mcp_server import call_tool, TOOLS

    assert any(t["name"] == "butian_member_import" for t in TOOLS)
    source = tmp_path / "memory.db"
    _make_source(source)
    service = ButianService(tmp_path / "home")
    try:
        with pytest.raises(ValueError, match="admin_authorization_required"):
            call_tool(service, "butian_member_import", {"source_path": str(source)})
        result = service.import_members(str(source))
        assert result["imported"] == 2
        assert len(service.list_migration_runs()) == 1
    finally:
        service.close()


def test_historical_memory_migration_is_forbidden(tmp_path):
    service = ButianService(tmp_path / "home")
    try:
        with pytest.raises(ValueError, match="historical_memory_migration_forbidden"):
            service.import_members(str(tmp_path / "missing.db"), import_memories=True)
    finally:
        service.close()
