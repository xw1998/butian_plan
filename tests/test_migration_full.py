from __future__ import annotations

import pytest
from butian.runtime.service import ButianService


def test_full_migration_surface_is_disabled(tmp_path):
    service = ButianService(tmp_path / "home")
    try:
        with pytest.raises(ValueError, match="historical_identity_migration_out_of_scope"):
            service.preflight_members(tmp_path / "must-not-be-read.db")
        with pytest.raises(ValueError, match="historical_identity_migration_out_of_scope"):
            service.import_members(tmp_path / "must-not-be-read.db")
        assert not (tmp_path / "must-not-be-read.db").exists()
        assert service.list_migration_runs() == []
    finally:
        service.close()
