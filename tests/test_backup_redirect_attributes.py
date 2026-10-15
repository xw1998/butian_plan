"""Attribute-level controls; these do not replace the real junction test."""
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from butian.runtime.backup import _is_redirect, _scan_tree


@pytest.mark.parametrize('mode,attributes,expected', [
    (stat.S_IFDIR, 0x400, True),
    (stat.S_IFREG, 0x400, True),
    (stat.S_IFLNK, 0, True),
    (stat.S_IFDIR, 0x10, False),
    (stat.S_IFREG, 0x20, False),
])
def test_redirect_attributes(monkeypatch, mode, attributes, expected):
    monkeypatch.setattr(Path, 'lstat', lambda self: SimpleNamespace(
        st_mode=mode, st_file_attributes=attributes))
    assert _is_redirect(Path('unused')) is expected


def test_scan_rejects_reparse_directory_before_descent(tmp_path, monkeypatch):
    redirect = tmp_path / 'redirect'
    redirect.mkdir()
    original = Path.lstat

    def metadata(path):
        if path == redirect:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original(path)

    monkeypatch.setattr(Path, 'lstat', metadata)
    with pytest.raises(ValueError, match='backup_symlink_not_allowed'):
        _scan_tree(tmp_path, allow_manifest=True)


def test_missing_path_is_not_redirect(tmp_path):
    assert not _is_redirect(tmp_path / 'missing')
