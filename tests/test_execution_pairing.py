import os

import pytest

from butian.runtime.execution_pairing import create_pairing, load_pairing


def test_pairing_returns_only_public_identity_and_is_not_overwritten(tmp_path):
    if os.name != 'nt':
        tmp_path.chmod(0o700)
    path = tmp_path / 'pairing.bin'
    result = create_pairing(path, 'executor-test')
    assert set(result) == {'executor_id', 'credential_protection'}
    identity, key = load_pairing(path)
    assert identity == 'executor-test'
    assert len(key) == 32
    before = path.read_bytes()
    with pytest.raises(ValueError, match='executor_pairing_path_invalid'):
        create_pairing(path, 'other')
    assert path.read_bytes() == before
    if os.name == 'nt':
        assert b'executor-test' not in before


def test_invalid_material_never_appears_in_error(tmp_path):
    path = tmp_path / 'pairing.bin'
    path.write_bytes(b'not-protected-sensitive-value')
    if os.name != 'nt':
        path.chmod(0o600)
    with pytest.raises(ValueError, match='executor_pairing_protection_invalid'):
        load_pairing(path)


def test_local_path_rejects_parent_reparse_point(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path
    from butian.runtime.execution_pairing import validate_local_path
    original = Path.lstat
    parent = tmp_path / 'redirected'
    def fake_lstat(self):
        if self == parent:
            return SimpleNamespace(st_mode=0o40700, st_file_attributes=0x400)
        return original(self)
    monkeypatch.setattr(Path, 'lstat', fake_lstat)
    with pytest.raises(ValueError, match='unsafe_parent'):
        validate_local_path(parent / 'nested' / 'connection.json', 'unsafe_parent')
