from butian import cli, mcp_server


def test_init_and_member_use_explicit_home(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    fallback = tmp_path / "fallback"
    monkeypatch.setenv("BUTIAN_HOME", str(fallback))
    assert cli.main(["init"], home_dir=explicit) == 0
    assert (explicit / "team.db").exists()
    assert not fallback.exists()
    assert cli.main(["member", "add", "member", "Member"], home_dir=explicit) == 0
    from butian.runtime.store import Store
    store = Store(explicit / "team.db")
    try:
        assert store.db.execute("SELECT member_id FROM members WHERE member_id='member'").fetchone()
    finally:
        store.close()


def test_mcp_and_serve_use_explicit_home(tmp_path, monkeypatch):
    from butian.runtime import http_api
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("BUTIAN_HOME", str(tmp_path / "fallback"))
    calls = []
    monkeypatch.setattr(mcp_server, "run", lambda home: calls.append(home))
    monkeypatch.setattr(http_api, "serve", lambda home, host, port: calls.append(home))
    assert cli.main(["mcp"], home_dir=explicit) == 0
    assert cli.main(["serve"], home_dir=explicit) == 0
    assert calls == [explicit, explicit]
