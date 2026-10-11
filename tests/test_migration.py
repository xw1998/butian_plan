from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError

import pytest

from butian.runtime.service import ButianService


def test_historical_identity_migration_is_out_of_scope_and_does_not_touch_source(tmp_path):
    source = tmp_path / "must-not-be-read.db"
    service = ButianService(tmp_path / "home")
    try:
        for call in (
            lambda: service.preflight_members(str(source)),
            lambda: service.import_members(str(source)),
        ):
            with pytest.raises(ValueError, match="historical_identity_migration_out_of_scope"):
                call()
        assert not source.exists()
        assert service.list_migration_runs() == []
    finally:
        service.close()


def test_http_historical_migration_routes_fail_closed(tmp_path):
    from butian.runtime.http_api import serve
    home = tmp_path / "home"
    thread = threading.Thread(target=serve, kwargs={"home": home, "host": "127.0.0.1", "port": 8798}, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            urlrequest.urlopen("http://127.0.0.1:8798/api/status", timeout=1).read()
            break
        except Exception:
            threading.Event().wait(0.05)
    with pytest.raises(HTTPError) as exc: urlrequest.urlopen("http://127.0.0.1:8798/api/migration/runs", timeout=3)
    assert exc.value.code == 409
    payload = json.dumps({"source_path": str(tmp_path / "must-not-be-read.db")}).encode()
    req = urlrequest.Request("http://127.0.0.1:8798/api/migration/import", data=payload, headers={"Content-Type": "application/json"})
    with pytest.raises(HTTPError) as exc: urlrequest.urlopen(req, timeout=3)
    assert exc.value.code == 409
    assert not (tmp_path / "must-not-be-read.db").exists()


def test_cli_and_mcp_historical_names_fail_closed(tmp_path):
    from butian import cli, mcp_server
    service = ButianService(tmp_path / "home")
    try:
        with pytest.raises(ValueError, match="historical_identity_migration_out_of_scope"):
            mcp_server.call_tool(service, "butian_member_import", {"source_path": str(tmp_path / "must-not-be-read.db")})
        assert not any(item["name"].startswith("butian_member_") for item in mcp_server.TOOLS)
        with pytest.raises(ValueError, match="historical_identity_migration_out_of_scope"):
            cli.main(["migrate", "runs"], home_dir=tmp_path / "cli-home")
    finally:
        service.close()
