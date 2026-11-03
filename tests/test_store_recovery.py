"""残留 -shm 不应该让 butian 在 MCP 握手前退出。

真实故障：~/.butian/team.db 旁边留着上一次崩溃的 team.db-shm，
`PRAGMA journal_mode=WAL` 抛 disk I/O error，进程直接死，宿主只看到管道 EOF。
Windows 上难以稳定自然复现（SQLite 多数情况下会自行重建索引），因此这里用
故障注入验证恢复路径本身，并逐条钉死"只动派生索引、不动 WAL、不删只挪"。
"""

import sqlite3

import pytest

from butian.runtime import store as store_module
from butian.runtime.store import Store, StoreUnavailable, _quarantine_stale_shm


def _seeded_db(path):
    store = Store(path)
    store.db.execute("INSERT INTO meta(key, value) VALUES ('probe', 'kept')")
    store.close()
    return path


def _fail_first_open(monkeypatch, message="disk I/O error"):
    calls = {"n": 0}
    original = Store._connect_and_initialize

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError(message)
        return original(self)

    monkeypatch.setattr(Store, "_connect_and_initialize", flaky)
    return calls


def test_quarantine_moves_shm_aside_and_never_touches_wal(tmp_path):
    db = tmp_path / "team.db"
    db.write_bytes(b"database")
    shm = tmp_path / "team.db-shm"
    wal = tmp_path / "team.db-wal"
    shm.write_bytes(b"derived index")
    wal.write_bytes(b"authoritative log")

    moved = _quarantine_stale_shm(db)

    assert moved is not None and moved.exists()
    assert moved.name.startswith("team.db-shm.stale-")
    assert moved.read_bytes() == b"derived index"   # 改名保留，不是删除
    assert not shm.exists()
    assert wal.read_bytes() == b"authoritative log"  # WAL 一个字节都不许动


def test_store_recovers_from_stale_shm_without_losing_rows(tmp_path, monkeypatch):
    db = _seeded_db(tmp_path / "team.db")
    (tmp_path / "team.db-shm").write_bytes(b"stale")
    calls = _fail_first_open(monkeypatch)

    store = Store(db)
    try:
        assert calls["n"] == 2
        assert store.recovered_sidecar is not None
        assert store.db.execute("SELECT value FROM meta WHERE key='probe'").fetchone()[0] == "kept"
        assert store.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        store.close()


def test_missing_shm_reports_actionable_error_instead_of_raw_io_error(tmp_path, monkeypatch):
    db = _seeded_db(tmp_path / "team.db")
    calls = _fail_first_open(monkeypatch)

    with pytest.raises(StoreUnavailable) as excinfo:
        Store(db)

    assert calls["n"] == 1  # 没有可挪的索引就不该盲目重试
    details = excinfo.value.details
    assert details["code"] == "store_unavailable"
    assert details["path"] == str(db)
    assert [item["action"] for item in details["next_actions"]]


def test_locked_shm_is_left_alone(tmp_path, monkeypatch):
    db = _seeded_db(tmp_path / "team.db")
    shm = tmp_path / "team.db-shm"
    shm.write_bytes(b"held by a live process")
    _fail_first_open(monkeypatch)

    def refused(*args, **kwargs):
        raise PermissionError("in use")

    monkeypatch.setattr(store_module.os, "replace", refused)
    with pytest.raises(StoreUnavailable):
        Store(db)
    assert shm.read_bytes() == b"held by a live process"


def test_unrelated_sql_errors_are_not_masked_as_sidecar_damage(tmp_path, monkeypatch):
    db = _seeded_db(tmp_path / "team.db")
    shm = tmp_path / "team.db-shm"
    shm.write_bytes(b"index")
    calls = _fail_first_open(monkeypatch, message="table meta already exists")

    with pytest.raises(StoreUnavailable):
        Store(db)
    assert calls["n"] == 1
    assert shm.exists()  # 不是 IO 类错误，就不该动用户的文件
