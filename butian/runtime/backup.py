from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

_FORMAT = "butian-runtime-backup-v2"
_SQLITE_NAMES = {"team.db", "memory.db", "experiments.db"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("backup_path_invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts) or (relative.parts and ":" in relative.parts[0]):
        raise ValueError("backup_path_invalid")
    if relative.as_posix() != value or value == "manifest.json":
        raise ValueError("backup_path_invalid")
    return relative


def _is_redirect(path: Path) -> bool:
    """Reject symlinks and Windows reparse points without following targets."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )

def _scan_tree(root: Path, *, allow_manifest: bool, ignore_sidecars: bool = False) -> tuple[list[Path], list[str]]:
    if _is_redirect(root):
        raise ValueError("backup_symlink_not_allowed")
    files: list[Path] = []
    directories: list[str] = []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        kept = []
        for name in sorted(dirnames):
            path = base / name
            if _is_redirect(path):
                raise ValueError("backup_symlink_not_allowed")
            kept.append(name)
            directories.append(path.relative_to(root).as_posix())
        dirnames[:] = kept
        for name in sorted(filenames):
            path = base / name
            if _is_redirect(path) or not path.is_file():
                raise ValueError("backup_symlink_not_allowed")
            if ".backup-staging-" in name or ".restore-staging-" in name:
                continue
            if ignore_sidecars and name.endswith(("-wal", "-shm")):
                continue
            if not allow_manifest and name == "manifest.json" and path.parent == root:
                continue
            files.append(path)
    return sorted(files), sorted(directories)


def _is_sqlite(path: Path) -> bool:
    return path.name in _SQLITE_NAMES


def _sqlite_state(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise ValueError("backup_database_integrity_failed")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        incomplete_operations = 0
        if "operations" in tables:
            incomplete_operations = connection.execute("SELECT COUNT(*) FROM operations WHERE state!='committed'").fetchone()[0]
        return {
            "integrity": integrity[0],
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
            "incomplete_operations": incomplete_operations,
        }
    finally:
        connection.close()


def _backup_sqlite(source: Path, target: Path) -> dict[str, Any]:
    source_db = sqlite3.connect(source, timeout=5.0)
    target_db = sqlite3.connect(target)
    try:
        source_db.execute("PRAGMA busy_timeout=5000")
        source_db.backup(target_db)
        result = target_db.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError("backup_database_integrity_failed")
        target_db.commit()
        state = _sqlite_state(target)
        return state
    finally:
        try:
            source_db.rollback()
        except sqlite3.Error:
            pass
        target_db.close()
        source_db.close()


def _load_manifest(backup: Path) -> dict[str, Any]:
    manifest_path = backup / "manifest.json"
    if not backup.is_dir() or _is_redirect(manifest_path) or not manifest_path.is_file():
        raise ValueError("backup_manifest_not_found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != _FORMAT:
        raise ValueError("backup_format_unsupported")
    expected = manifest.get("manifest_sha256")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    actual = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if expected != actual:
        raise ValueError("backup_manifest_checksum_mismatch")
    return manifest


def _manifest_entries(backup: Path, manifest: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    entries: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("files", []):
        if not isinstance(entry, dict):
            raise ValueError("backup_manifest_entry_invalid")
        relative = _safe_relative(entry.get("path"))
        key = relative.as_posix()
        if key in entries or not isinstance(entry.get("size"), int) or entry["size"] < 0:
            raise ValueError("backup_manifest_entry_invalid")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("backup_manifest_entry_invalid")
        path = backup.joinpath(*relative.parts)
        if _is_redirect(path) or not path.is_file():
            raise ValueError("backup_file_missing")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError("backup_file_missing") from exc
        if backup not in resolved.parents or not resolved.is_file():
            raise ValueError("backup_path_invalid")
        if resolved.stat().st_size != entry["size"] or _sha256(resolved) != digest:
            raise ValueError("backup_file_checksum_mismatch")
        if _is_sqlite(resolved):
            state = _sqlite_state(resolved)
            if state["incomplete_operations"]:
                raise ValueError("backup_incomplete_operation")
        entries[key] = entry
    _reject_sqlite_sidecars(backup)
    actual_files, directories = _scan_tree(backup, allow_manifest=False)
    actual = {path.relative_to(backup).as_posix() for path in actual_files}
    listed = set(entries)
    if actual != listed:
        raise ValueError("backup_manifest_file_set_mismatch")
    expected_directories = set(manifest.get("directories", []))
    if any(_safe_relative(name).as_posix() != name for name in expected_directories):
        raise ValueError("backup_manifest_directory_invalid")
    actual_directories = set(directories)
    if actual_directories != expected_directories:
        raise ValueError("backup_manifest_directory_set_mismatch")
    return entries, listed


def _reject_sqlite_sidecars(root: Path) -> None:
    for path in root.rglob("*"):
        if _is_redirect(path):
            raise ValueError("backup_symlink_not_allowed")
        if path.is_file() and path.name.endswith(("-wal", "-shm")):
            raise ValueError("backup_sqlite_sidecar_present")


def _check_cross_database_state(root: Path, *, reject_sidecars: bool = True, allow_incomplete: bool = False) -> dict[str, Any]:
    if reject_sidecars:
        _reject_sqlite_sidecars(root)
    databases = []
    for path in sorted(root.rglob("*.db")):
        if path.name not in _SQLITE_NAMES:
            continue
        state = _sqlite_state(path)
        if state["incomplete_operations"] and not allow_incomplete:
            raise ValueError("backup_incomplete_operation")
        databases.append({"path": path.relative_to(root).as_posix(), "integrity": state["integrity"], "journal_mode": state["journal_mode"], "incomplete_operations": state["incomplete_operations"]})
    return {"database_count": len(databases), "databases": databases}


def create_backup(home: str | Path, destination: str | Path) -> dict[str, Any]:
    home = Path(home)
    if _is_redirect(home):
        raise ValueError("backup_symlink_not_allowed")
    home = home.resolve()
    destination = Path(destination).resolve()
    if not home.is_dir():
        raise ValueError("backup_source_not_found")
    if destination == home or home in destination.parents:
        raise ValueError("backup_destination_overlaps_source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("backup_destination_exists")
    staging = destination.parent / f".backup-staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        sources, source_directories = _scan_tree(home, allow_manifest=True, ignore_sidecars=True)
        database_state = _check_cross_database_state(home, reject_sidecars=False, allow_incomplete=True)
        if any(item["incomplete_operations"] for item in database_state["databases"]):
            raise ValueError("backup_incomplete_operation")
        evidence_files = _evidence_files(home)
        source_by_relative = {path.relative_to(home).as_posix(): path for path in sources}
        for relative, source in evidence_files.items():
            source_by_relative.setdefault(relative, source)
        sources = sorted(source_by_relative.values())
        for relative in source_directories:
            (staging / Path(*PurePosixPath(relative).parts)).mkdir(parents=True, exist_ok=True)
        copied_database_state = []
        source_names = {path.name for path in sources}
        for path in home.rglob("*"):
            if path.is_file() and path.name.endswith(("-wal", "-shm")):
                database_name = path.name[:-4]
                if database_name not in _SQLITE_NAMES or database_name not in source_names:
                    raise ValueError("backup_unrelated_sqlite_sidecar_present")
        for source in sources:
            relative = source.relative_to(home)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if _is_sqlite(source):
                db_state = _backup_sqlite(source, target)
                copied_database_state.append({"path": relative.as_posix(), **db_state})
            else:
                shutil.copy2(source, target)
        entries = []
        _reject_sqlite_sidecars(staging)
        copied, _ = _scan_tree(staging, allow_manifest=True)
        for path in copied:
            entries.append({"path": path.relative_to(staging).as_posix(), "size": path.stat().st_size, "sha256": _sha256(path)})
        copied_directories = {parent.as_posix() for item in entries for parent in list(PurePosixPath(item["path"]).parents) if parent.as_posix() != "."}
        manifest = {
            "format": _FORMAT,
            "created_at": time.time(),
            "source": str(home),
            "files": entries,
            "directories": sorted(copied_directories | set(source_directories)),
            "consistency": {"mode": "sqlite-online-backup-per-db", "databases": copied_database_state, "incomplete_operations": 0, "source_database_inventory": database_state["databases"]},
        }
        manifest["manifest_sha256"] = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        verify_backup(staging)
        os.replace(staging, destination)
        return {"status": "created", "path": str(destination), "file_count": len(entries), "manifest_sha256": manifest["manifest_sha256"], "consistency": manifest["consistency"]}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_backup(backup: str | Path) -> dict[str, Any]:
    backup = Path(backup)
    if _is_redirect(backup):
        raise ValueError("backup_symlink_not_allowed")
    backup = backup.resolve()
    manifest = _load_manifest(backup)
    entries, listed = _manifest_entries(backup, manifest)
    database_state = _check_cross_database_state(backup, reject_sidecars=True)
    declared = manifest.get("consistency", {}).get("databases", [])
    actual = database_state["databases"]
    if declared != actual:
        raise ValueError("backup_database_inventory_mismatch")
    return {"status": "verified", "path": str(backup), "file_count": len(entries), "manifest_sha256": manifest["manifest_sha256"], "consistency": manifest.get("consistency", {})}


def _logical_path(raw: str) -> PureWindowsPath | PurePosixPath:
    if "\\\\" in raw or (len(raw) >= 2 and raw[1] == ":"):
        return PureWindowsPath(raw)
    return PurePosixPath(raw)


def _evidence_files(home: Path) -> dict[str, Path]:
    team = home / "team.db"
    if not team.is_file():
        return {}
    connection = sqlite3.connect(team)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "evidence" not in tables:
            return {}
        result = {}
        for (raw,) in connection.execute("SELECT path FROM evidence"):
            logical = _logical_path(raw)
            source_root = _logical_path(str(home))
            try:
                relative = logical.relative_to(source_root)
            except ValueError as exc:
                raise ValueError("backup_evidence_outside_source") from exc
            source = home.joinpath(*relative.parts)
            if _is_redirect(source) or not source.is_file():
                raise ValueError("backup_evidence_reference_invalid")
            result[PurePosixPath(*relative.parts).as_posix()] = source
        return result
    finally:
        connection.close()


def _rebind_database_paths(root: Path, old_home: str, new_home: Path) -> int:
    old_root = _logical_path(old_home)
    changed = 0
    for database in root.rglob("*.db"):
        if database.name not in _SQLITE_NAMES:
            continue
        connection = sqlite3.connect(database)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, column in (("evidence", "path"), ("tool_versions", "source_path")):
                if table not in tables:
                    continue
                columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    continue
                rows = connection.execute(f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL").fetchall()
                for rowid, raw in rows:
                    candidate = _logical_path(raw)
                    if not candidate.is_absolute():
                        continue
                    try:
                        relative = candidate.relative_to(old_root)
                    except ValueError:
                        if table == "evidence":
                            raise ValueError("restore_evidence_reference_invalid")
                        continue
                    rebound = new_home.joinpath(*relative.parts)
                    connection.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (str(rebound), rowid))
                    changed += 1
            connection.commit()
        finally:
            connection.close()
    return changed


def _validate_rebound_references(root: Path, final_root: Path) -> int:
    checked = 0
    team = root / "team.db"
    if team.is_file():
        connection = sqlite3.connect(team)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "evidence" in tables:
                for raw, digest, size in connection.execute("SELECT path,sha256,size FROM evidence"):
                    path = _logical_path(raw)
                    expected_root = _logical_path(str(final_root))
                    try:
                        relative = path.relative_to(expected_root)
                    except ValueError as exc:
                        raise ValueError("restore_evidence_reference_invalid") from exc
                    staged_path = root.joinpath(*relative.parts)
                    if not staged_path.is_file() or staged_path.stat().st_size != size or _sha256(staged_path) != digest:
                        raise ValueError("restore_evidence_reference_invalid")
                    checked += 1
        finally:
            connection.close()
    return checked


def restore_backup(backup: str | Path, destination: str | Path) -> dict[str, Any]:
    backup = Path(backup)
    if _is_redirect(backup):
        raise ValueError("backup_symlink_not_allowed")
    backup = backup.resolve()
    verification = verify_backup(backup)
    manifest = _load_manifest(backup)
    entries, _ = _manifest_entries(backup, manifest)
    destination = Path(destination).resolve()
    if destination == backup or backup in destination.parents or destination in backup.parents:
        raise ValueError("restore_destination_overlaps_backup")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("restore_destination_not_empty")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".restore-staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        directories = manifest.get("directories", [])
        if not isinstance(directories, list) or len(set(directories)) != len(directories):
            raise ValueError("backup_manifest_directory_invalid")
        for name in sorted(directories, key=lambda value: (value.count("/"), value)):
            relative = _safe_relative(name)
            (staging / Path(*relative.parts)).mkdir(parents=True, exist_ok=True)
        for key, entry in entries.items():
            relative = _safe_relative(key)
            source = backup.joinpath(*relative.parts)
            target = staging.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if _is_sqlite(source):
                _backup_sqlite(source, target)
            else:
                shutil.copy2(source, target)
        _rebind_database_paths(staging, manifest["source"], destination)
        state = _check_cross_database_state(staging)
        evidence_count = _validate_rebound_references(staging, destination)
        staged_files, staged_dirs = _scan_tree(staging, allow_manifest=True)
        if {path.relative_to(staging).as_posix() for path in staged_files} != set(entries):
            raise ValueError("restore_manifest_file_set_mismatch")
        expected_dirs = set(manifest.get("directories", []))
        if set(staged_dirs) != expected_dirs:
            raise ValueError("restore_manifest_directory_set_mismatch")
        if destination.exists():
            destination.rmdir()
        os.replace(staging, destination)
        return {"status": "restored", "path": str(destination), "file_count": verification["file_count"], "manifest_sha256": verification["manifest_sha256"], "consistency": state, "rebound_evidence_count": evidence_count}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
