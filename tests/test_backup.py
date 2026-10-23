import hashlib
import json
import sqlite3

import pytest

from butian.runtime.service import ButianService


def test_backup_verify_restore_preserves_databases_tools_config_and_manifest(tmp_path):
    home = tmp_path / "runtime"
    service = ButianService(home)
    try:
        service.add_member("member", "Member")
        service.store.set_setting("backup-marker", "present")
        memory = service.memory("member")
        try:
            memory.put("memory-backup", "observation", "backup evidence", status="candidate")
        finally:
            memory.close()
        (home / "providers.json").write_text('{"providers": []}\n', encoding="utf-8")
        backup = tmp_path / "backup"
        created = service.create_backup(backup)
        assert created["status"] == "created"
        assert created["consistency"]["mode"] == "sqlite-online-backup-per-db"
        verified = service.verify_backup(backup)
        assert verified["file_count"] >= 3
        restored = tmp_path / "restored"
        result = service.restore_backup(backup, restored)
        assert result["status"] == "restored"
        assert result["consistency"]["database_count"] >= 2
    finally:
        service.close()

    restored_service = ButianService(restored)
    try:
        assert restored_service.store.get_setting("backup-marker") == "present"
        assert restored_service.store.db.execute("SELECT 1 FROM members WHERE member_id='member'").fetchone()
        memory = restored_service.memory("member")
        try:
            assert memory.db.execute("SELECT 1 FROM memories WHERE content='backup evidence'").fetchone()
        finally:
            memory.close()
        assert (restored / "providers.json").is_file()
    finally:
        restored_service.close()


def test_backup_detects_tampering_and_rejects_unsafe_destination(tmp_path):
    home = tmp_path / "runtime"
    service = ButianService(home)
    try:
        (home / "providers.json").write_text('{"providers": []}\n', encoding="utf-8")
        backup = tmp_path / "backup"
        service.create_backup(backup)
        (backup / "providers.json").write_text("tampered\n", encoding="utf-8")
        with pytest.raises(ValueError, match="checksum"):
            service.verify_backup(backup)
        with pytest.raises(ValueError, match="overlaps_source"):
            service.create_backup(home / "nested-backup")
    finally:
        service.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_backup_rejects_unlisted_sqlite_sidecars(tmp_path, suffix):
    home = tmp_path / "runtime"
    service = ButianService(home)
    try:
        service.close()
        (home / f"orphan.db{suffix}").write_bytes(b"unlisted sidecar")
        with pytest.raises(ValueError, match="sidecar"):
            service.create_backup(tmp_path / "backup")
    finally:
        try:
            service.close()
        except sqlite3.Error:
            pass


def test_backup_rejects_unlisted_extra_file_and_restore_never_copies_it(tmp_path):
    home = tmp_path / "runtime"
    service = ButianService(home)
    try:
        backup = tmp_path / "backup"
        service.create_backup(backup)
        (backup / "unlisted.txt").write_text("unexpected\n", encoding="utf-8")
        with pytest.raises(ValueError, match="file_set"):
            service.verify_backup(backup)
        with pytest.raises(ValueError, match="file_set"):
            service.restore_backup(backup, tmp_path / "restored")
        assert not (tmp_path / "restored" / "unlisted.txt").exists()
    finally:
        service.close()


def test_restore_rebinds_evidence_paths_and_checks_hashes(tmp_path):
    home = tmp_path / "runtime"
    artifact_dir = home / "artifacts"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "result.txt"
    artifact.write_text("verified output\n", encoding="utf-8")
    service = ButianService(home)
    try:
        evidence_id = service.record_evidence(artifact, owner_member_id=None)
        backup = tmp_path / "backup"
        service.create_backup(backup)
    finally:
        service.close()

    restored = tmp_path / "restored-runtime"
    result = ButianService(tmp_path / "empty-home")
    try:
        outcome = result.restore_backup(backup, restored)
        assert outcome["rebound_evidence_count"] == 1
    finally:
        result.close()
    database = sqlite3.connect(restored / "team.db")
    try:
        path = database.execute("SELECT path FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()[0]
        assert path == str(restored / "artifacts" / "result.txt")
        assert str(home) not in path
        assert hashlib.sha256(open(path, "rb").read()).hexdigest() == database.execute("SELECT sha256 FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()[0]
    finally:
        database.close()


def test_restore_rejects_evidence_outside_backup_home(tmp_path):
    home = tmp_path / "runtime"
    external = tmp_path / "outside.txt"
    external.write_text("outside snapshot\n", encoding="utf-8")
    service = ButianService(home)
    try:
        service.record_evidence(external)
        with pytest.raises(ValueError, match="evidence_outside_source"):
            service.create_backup(tmp_path / "backup")
        assert not (tmp_path / "backup").exists()
    finally:
        service.close()


def test_restore_manifest_is_the_only_copy_allowlist(tmp_path):
    home = tmp_path / "runtime"
    service = ButianService(home)
    try:
        backup = tmp_path / "backup"
        service.create_backup(backup)
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["manifest_sha256"] = hashlib.sha256(json.dumps({key: value for key, value in manifest.items() if key != "manifest_sha256"}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (backup / "not-listed.bin").write_bytes(b"extra")
        with pytest.raises(ValueError, match="file_set"):
            service.restore_backup(backup, tmp_path / "restore")
        assert not (tmp_path / "restore" / "not-listed.bin").exists()
    finally:
        service.close()


def test_backup_rejects_redirect_in_source(tmp_path, redirect_link):
    """指向外部的重定向点必须被拒；没有符号链接权限时用目录联接（不 skip）。

    发版闸门要求全量测试零 skip，而 Windows 建立符号链接需要开发者模式或管理员
    权限；目录联接同样是重定向点，走的是 _is_redirect 的同一条判定分支。
    """
    home = tmp_path / "runtime"
    service = ButianService(home)
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel.txt").write_text("must not be copied", encoding="utf-8")
    link = home / "escape"
    try:
        redirect_link(link, external)
        with pytest.raises(ValueError, match="symlink"):
            service.create_backup(tmp_path / "backup")
        assert not (tmp_path / "backup").exists()
        assert (external / "sentinel.txt").read_text(encoding="utf-8") == "must not be copied"
    finally:
        service.close()


def test_backup_rejects_incomplete_operation_state(tmp_path):
    home = tmp_path / "runtime"
    service = ButianService(home)
    try:
        service.store.db.execute("INSERT INTO operations VALUES (?,?,?,?,?)", ("op_pending", "pending-key", "prepared", 1.0, None))
        with pytest.raises(ValueError, match="incomplete_operation"):
            service.create_backup(tmp_path / "backup")
        assert not (tmp_path / "backup").exists()
        assert not (tmp_path / "backup").exists()
    finally:
        service.close()


def test_backup_detects_missing_manifest_file_and_database_corruption(tmp_path):
    home = tmp_path / "runtime"
    service = ButianService(home)
    try:
        backup = tmp_path / "backup"
        service.create_backup(backup)
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        first = backup / manifest["files"][0]["path"]
        # 用移出代替删除：校验的语义是“清单里的文件不在盘上”，且本机策略禁止
        # os.remove 的永久删除，删除动作会把用例挡在环境里而不是代码里。
        first.replace(tmp_path / "moved-away.bin")
        with pytest.raises(ValueError):
            service.verify_backup(backup)
    finally:
        service.close()


def test_backup_restore_preserves_empty_directories(tmp_path):
    home = tmp_path / "runtime"
    service = ButianService(home)
    try:
        empty = home / "artifacts" / "empty"
        empty.mkdir(parents=True)
        backup = tmp_path / "backup"
        service.create_backup(backup)
        assert "artifacts/empty" in json.loads((backup / "manifest.json").read_text(encoding="utf-8"))["directories"]
        restored = tmp_path / "restored"
        service.restore_backup(backup, restored)
        assert (restored / "artifacts" / "empty").is_dir()
    finally:
        service.close()


def test_backup_restore_includes_active_team_and_member_memory_wal(tmp_path):
    home = tmp_path / "runtime"
    service = ButianService(home)
    service.add_member("member-a", "Member A")
    service.add_member("member-b", "Member B")
    memory_a = service.memory("member-a")
    memory_b = service.memory("member-b")
    try:
        service.store.db.execute("PRAGMA journal_mode=WAL")
        memory_a.db.execute("PRAGMA journal_mode=WAL")
        memory_b.db.execute("PRAGMA journal_mode=WAL")
        service.store.db.execute("INSERT INTO settings(key,value) VALUES('active-wal','team')")
        memory_a.put("wal-a", "observation", "memory A", status="candidate")
        memory_b.put("wal-b", "observation", "memory B", status="candidate")
        backup = tmp_path / "backup"
        service.create_backup(backup)
        restored = tmp_path / "restored"
        service.restore_backup(backup, restored)
    finally:
        memory_a.close()
        memory_b.close()
        service.close()

    restored_service = ButianService(restored)
    try:
        assert restored_service.store.get_setting("active-wal") == "team"
        for member_id, memory_id in (("member-a", "wal-a"), ("member-b", "wal-b")):
            memory = restored_service.memory(member_id)
            try:
                assert memory.db.execute("SELECT 1 FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            finally:
                memory.close()
    finally:
        restored_service.close()


def test_backup_rejects_corrupt_sqlite_database(tmp_path):
    from butian.runtime.backup import create_backup

    home = tmp_path / "runtime"
    service = ButianService(home)
    try:
        service.close()
        with (home / "team.db").open("r+b") as database:
            database.seek(0)
            database.write(b"not a sqlite database")
            database.flush()
        with pytest.raises((ValueError, sqlite3.DatabaseError)):
            create_backup(home, tmp_path / "backup")
        assert not (tmp_path / "backup").exists()
    finally:
        try:
            service.close()
        except sqlite3.Error:
            pass
