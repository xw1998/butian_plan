from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError

import pytest

from butian import mcp_server
from butian.runtime import http_api
from butian.runtime.service import ButianService
from butian.runtime.experiments import ExperimentStore


def test_mcp_tool_schemas_include_verification_and_execution_limits():
    schemas = {item["name"]: item["inputSchema"] for item in mcp_server.TOOLS}
    execute = schemas["butian_tool_execute"]["properties"]
    verify = schemas["butian_tool_verify"]["properties"]
    assert {"verification_only", "timeout_seconds", "max_output_bytes"} <= execute.keys()
    assert {"execution_id", "input_hash", "expected_output", "evidence_ids", "environment", "task_id"} <= verify.keys()


def test_mcp_tool_execute_forwards_verification_and_resource_limits(monkeypatch):
    captured = {}
    class StubService:
        store = type("Store", (), {"authenticate_member": staticmethod(lambda member, token: True)})()
        def require_member_access(self, target, actor_member_id=None, token=None):
            assert target == actor_member_id == "m1" and token == "secret"
        def execute_tool(self, member_id, tool_id, version, input_data, **options):
            captured.update(member_id=member_id, tool_id=tool_id, version=version, input_data=input_data, **options)
            return {"status": "dry_run"}
    result = mcp_server.call_tool(StubService(), "butian_tool_execute", {
        "member_id": "m1", "actor_member_id": "m1", "token": "secret", "tool_id": "t1", "version": 2,
        "input": {"x": 1}, "verification_only": True, "timeout_seconds": 2.5, "max_output_bytes": 4096,
    })
    assert result["status"] == "dry_run"
    assert captured["verification_only"] is True
    assert captured["timeout_seconds"] == 2.5
    assert captured["max_output_bytes"] == 4096


def test_http_tool_verify_authenticates_and_forwards_binding_fields(tmp_path, monkeypatch):
    service = ButianService(tmp_path)
    service.add_member("m1", "Member")
    service.store.set_member_token("m1", "secret")
    captured = {}
    def verify_tool(member_id, tool_id, version, outcome, **details):
        captured.update(member_id=member_id, tool_id=tool_id, version=version, outcome=outcome, **details)
        return {"verification_id": "vfy-1", "status": "candidate"}
    monkeypatch.setattr(service, "verify_tool", verify_tool)
    http_api.ApiHandler.service = service
    http_api.ApiHandler.experiments = None
    http_api.ApiHandler.web_root = Path(__file__).resolve().parents[1] / "web"
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    body = {
        "member_id": "m1", "actor_member_id": "m1", "token": "secret", "tool_id": "t1", "version": 3,
        "outcome": "success", "independent_key": "case-1", "execution_id": "exec-1", "input_hash": "abc",
        "expected_output": {"ok": True}, "task_id": "task-1", "environment": {"os": "test"}, "evidence_ids": ["e1"],
    }
    try:
        unauthenticated = {**body, "token": ""}
        req = urlrequest.Request(base + "/api/tools/verify", data=json.dumps(unauthenticated).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(HTTPError) as missing_auth:
            urlrequest.urlopen(req, timeout=3)
        assert missing_auth.value.code == 401
        req = urlrequest.Request(base + "/api/tools/verify", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-Butian-Member": "m1", "X-Butian-Token": "secret"}, method="POST")
        with urlrequest.urlopen(req, timeout=3) as response:
            payload = json.loads(response.read())
        assert payload["status"] == "ok"
        assert payload["data"]["verification_id"] == "vfy-1"
        assert captured["execution_id"] == "exec-1"
        assert captured["input_hash"] == "abc"
        assert captured["expected_output"] == {"ok": True}
        assert captured["evidence_ids"] == ["e1"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        service.close()


@pytest.mark.parametrize("path", [
    "/api/tasks",
    "/api/tasks/tree",
    "/api/questions",
    "/api/sessions",
    "/api/members",
    "/api/hypotheses",
    "/api/knowledge/claims",
    "/api/knowledge/runs",
    "/api/events",
    "/api/experiments",
    "/api/experiments/exp-1/failures",
])
def test_http_sensitive_gets_require_member_auth(tmp_path, path):
    service = ButianService(tmp_path)
    service.add_member("m1", "Member", role="bootstrap_admin")
    service.store.set_member_token("m1", "secret")
    http_api.ApiHandler.service = service
    http_api.ApiHandler.experiments = ExperimentStore(Path(tmp_path) / "experiments.db")
    http_api.ApiHandler.web_root = Path(__file__).resolve().parents[1] / "web"
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(HTTPError) as missing_auth:
            urlrequest.urlopen(base + path, timeout=3)
        assert missing_auth.value.code == 401
        req = urlrequest.Request(base + path, headers={"X-Butian-Member": "m1", "X-Butian-Token": "secret"})
        with urlrequest.urlopen(req, timeout=3) as response:
            assert response.status in (200, 404)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        service.close()


def test_http_member_creation_requires_auth(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("m1", "Member", role="bootstrap_admin")
    service.store.set_member_token("m1", "secret")
    http_api.ApiHandler.service = service
    http_api.ApiHandler.experiments = None
    http_api.ApiHandler.web_root = Path(__file__).resolve().parents[1] / "web"
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        req = urlrequest.Request(base + "/api/members", data=json.dumps({"member_id":"m2","name":"Member 2"}).encode(), headers={"Content-Type":"application/json"}, method="POST")
        with pytest.raises(HTTPError) as error:
            urlrequest.urlopen(req, timeout=3)
        assert error.value.code == 401
        authorized = urlrequest.Request(base + "/api/members", data=json.dumps({"member_id":"m2","name":"Member 2"}).encode(), headers={"Content-Type":"application/json", "X-Butian-Member":"m1", "X-Butian-Token":"secret"}, method="POST")
        with urlrequest.urlopen(authorized, timeout=3) as response:
            assert response.status == 201
        assert service.store.db.execute("SELECT 1 FROM members WHERE member_id='m2'").fetchone()
        service.store.set_member_token("m2", "secret2")
        denied_request = urlrequest.Request(base + "/api/members", data=json.dumps({"member_id":"m3","name":"Member 3"}).encode(), headers={"Content-Type":"application/json", "X-Butian-Member":"m2", "X-Butian-Token":"secret2"}, method="POST")
        with pytest.raises(HTTPError) as denied:
            urlrequest.urlopen(denied_request, timeout=3)
        assert denied.value.code == 403
        assert not service.store.db.execute("SELECT 1 FROM members WHERE member_id='m3'").fetchone()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3); service.close()

def test_mcp_escalation_schemas_only_require_action_fields():
    schemas = {item["name"]: item["inputSchema"] for item in mcp_server.TOOLS}
    assert "now" not in schemas["butian_tool_issue_escalations"]["properties"]
    assert "now" not in schemas["butian_tool_issue_escalation_claim"]["properties"]
    assert "now" not in schemas["butian_tool_issue_escalation_resolve"]["properties"]
    assert {"member_id", "actor_member_id", "token"} <= set(schemas["butian_tool_issue_escalations"]["required"])


def test_http_issue_escalations_require_auth_and_forward_dispatcher(tmp_path, monkeypatch):
    service = ButianService(tmp_path)
    service.add_member("dispatcher", "Dispatcher")
    service.store.set_member_token("dispatcher", "secret")
    monkeypatch.setattr(service, "list_tool_issue_escalations", lambda member_id, status=None: [{"work_item_id": "wi-1", "status": status or "open"}])
    http_api.ApiHandler.service = service
    http_api.ApiHandler.experiments = None
    http_api.ApiHandler.web_root = Path(__file__).resolve().parents[1] / "web"
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    payload = {"member_id": "dispatcher", "status": "open"}
    try:
        req = urlrequest.Request(base + "/api/tools/issues/escalations", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(HTTPError) as missing_auth:
            urlrequest.urlopen(req, timeout=3)
        assert missing_auth.value.code == 401
        req = urlrequest.Request(base + "/api/tools/issues/escalations", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "X-Butian-Member": "dispatcher", "X-Butian-Token": "secret"}, method="POST")
        with urlrequest.urlopen(req, timeout=3) as response:
            result = json.loads(response.read())
        assert result["status"] == "ok"
        assert result["data"]["work_items"] == [{"work_item_id": "wi-1", "status": "open"}]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        service.close()


def test_http_member_cannot_read_another_members_tasks_questions_or_sessions(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("m1", "Member 1")
    service.add_member("m2", "Member 2")
    service.store.set_member_token("m1", "secret-1")
    service.store.set_member_token("m2", "secret-2")
    own_session = service.create_session("solo", current_member_id="m1")["session_id"]
    other_session = service.create_session("solo", current_member_id="m2")["session_id"]
    other_task = service.store.create_task(other_session, "private task")
    service.store.db.execute(
        "INSERT INTO questions(question_id, task_id, revision, member_id, question, answer, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("q-private", other_task, 1, "m2", "private question", None, "open", 1.0),
    )
    service.store.db.commit()
    http_api.ApiHandler.service = service
    http_api.ApiHandler.experiments = None
    http_api.ApiHandler.web_root = Path(__file__).resolve().parents[1] / "web"
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {"X-Butian-Member": "m1", "X-Butian-Token": "secret-1"}
    try:
        for path, key, private_id in [
            ("/api/tasks", "tasks", other_task),
            ("/api/questions", "questions", "q-private"),
            ("/api/sessions", "sessions", other_session),
        ]:
            req = urlrequest.Request(base + path, headers=headers)
            with urlrequest.urlopen(req, timeout=3) as response:
                payload = json.loads(response.read())
            assert private_id not in {row.get("task_id", row.get("session_id", row.get("question_id"))) for row in payload["data"][key]}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        service.close()


def test_http_provider_cancel_requires_session_access(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("owner", "Owner")
    service.add_member("other", "Other")
    service.store.set_member_token("owner", "owner-token")
    service.store.set_member_token("other", "other-token")
    session_id = service.store.create_provider_session("owner", "manual", prompt="work")
    http_api.ApiHandler.service = service
    http_api.ApiHandler.experiments = None
    http_api.ApiHandler.web_root = Path(__file__).resolve().parents[1] / "web"
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        body = json.dumps({"provider_session_id": session_id}).encode()
        req = urlrequest.Request(base + "/api/providers/cancel", data=body, headers={"Content-Type": "application/json", "X-Butian-Member": "other", "X-Butian-Token": "other-token"}, method="POST")
        with pytest.raises(HTTPError) as denied:
            urlrequest.urlopen(req, timeout=3)
        assert denied.value.code == 403
        assert service.store.provider_session(session_id)["status"] == "running"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        service.close()


def test_http_provider_start_binds_authenticated_actor(tmp_path, monkeypatch):
    service = ButianService(tmp_path)
    service.add_member("m1", "Member 1")
    service.add_member("m2", "Member 2")
    service.store.set_member_token("m1", "secret1")
    service.store.set_member_token("m2", "secret2")
    captured = {}
    monkeypatch.setattr(service, "start_provider_task", lambda member_id, prompt, **options: captured.update(member_id=member_id, prompt=prompt, options=options) or {"status": "running"})
    http_api.ApiHandler.service = service
    http_api.ApiHandler.experiments = None
    http_api.ApiHandler.web_root = Path(__file__).resolve().parents[1] / "web"
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        payload = {"member_id": "m2", "prompt": "run"}
        req = urlrequest.Request(base + "/api/providers/start", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(HTTPError) as missing:
            urlrequest.urlopen(req, timeout=3)
        assert missing.value.code == 401
        req = urlrequest.Request(base + "/api/providers/start", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "X-Butian-Member": "m1", "X-Butian-Token": "secret1"}, method="POST")
        with pytest.raises(HTTPError) as foreign:
            urlrequest.urlopen(req, timeout=3)
        assert foreign.value.code == 403
        assert captured == {}
        payload["member_id"] = "m1"
        req = urlrequest.Request(base + "/api/providers/start", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "X-Butian-Member": "m1", "X-Butian-Token": "secret1"}, method="POST")
        with urlrequest.urlopen(req, timeout=3) as response:
            assert response.status == 201
        assert captured["member_id"] == "m1"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3); service.close()


def test_http_host_authorization_binds_authenticated_owner_and_supports_listing_revoke(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("owner", "Owner")
    service.add_member("other", "Other")
    service.store.set_member_token("owner", "owner-token")
    service.store.set_member_token("other", "other-token")
    tool_id = service.propose_tool("owner", "host", "host tool", reuse_lower=3, manual_cost_upper=10,
                                   call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2,
                                   risk_cost_upper=1)["tool_id"]
    manifest = {"input_contract": {"type": "object"}, "output_contract": {"type": "object"},
                "permissions": ["execute"], "failure_conditions": ["invalid_input"],
                "isolation": {"profile": "host_trusted"}, "entrypoint": ["python", "tool.py"]}
    service.add_tool_version("owner", tool_id, "print('{}')", manifest)
    http_api.ApiHandler.service = service
    http_api.ApiHandler.experiments = None
    http_api.ApiHandler.web_root = Path(__file__).resolve().parents[1] / "web"
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    def request(path, payload, member="owner", token="owner-token"):
        req = urlrequest.Request(base + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "X-Butian-Member": member,
                                          "X-Butian-Token": token}, method="POST")
        with urlrequest.urlopen(req, timeout=3) as response:
            return json.loads(response.read())
    try:
        with pytest.raises(HTTPError) as denied:
            request("/api/tools/host-authorizations/grant", {"member_id": "other", "tool_id": tool_id,
                    "version": 1, "capabilities": ["process.spawn"], "reason": "test"})
        assert denied.value.code == 403
        grant = request("/api/tools/host-authorizations/grant", {"tool_id": tool_id,
                "version": 1, "capabilities": ["process.spawn"], "reason": "test"})
        assert grant["data"]["owner_member_id"] == "owner"
        listed = request("/api/tools/host-authorizations/list", {})
        assert listed["data"]["authorizations"][0]["authorization_id"] == grant["data"]["authorization_id"]
        revoked = request("/api/tools/host-authorizations/revoke", {
                 "authorization_id": grant["data"]["authorization_id"], "reason": "done"})
        assert revoked["data"]["status"] == "revoked"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3); service.close()


def test_host_authorization_capabilities_must_cover_manifest(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("m1", "Member 1")
    try:
        manifest = {"input_contract": {"type": "object"}, "output_contract": {"type": "object"},
                    "permissions": ["execute"], "failure_conditions": ["invalid_input"],
                    "host_capabilities": ["network"], "isolation": {"profile": "host_trusted"},
                    "entrypoint": ["python", "tool.py"]}
        tool_id = service.propose_tool("m1", "needs network", "coverage", reuse_lower=3, manual_cost_upper=10,
                                       call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2,
                                       risk_cost_upper=1)["tool_id"]
        service.add_tool_version("m1", tool_id, "print('{}')", manifest)
        service.grant_host_authorization("m1", tool_id, 1, capabilities=["process.spawn"], reason="wrong")
        with pytest.raises(ValueError, match="host_authorization_capability_missing"):
            service.execute_tool("m1", tool_id, 1, {}, verification_only=True)
    finally:
        service.close()


def test_http_session_creation_requires_member_auth(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("m1", "Member")
    service.store.set_member_token("m1", "secret")
    http_api.ApiHandler.service = service
    http_api.ApiHandler.experiments = None
    http_api.ApiHandler.web_root = Path(__file__).resolve().parents[1] / "web"
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        body = json.dumps({"mode": "solo", "current_member_id": "m1"}).encode()
        req = urlrequest.Request(base + "/api/sessions", data=body, headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(HTTPError) as denied:
            urlrequest.urlopen(req, timeout=3)
        assert denied.value.code == 401
        req = urlrequest.Request(base + "/api/sessions", data=body, headers={"Content-Type": "application/json", "X-Butian-Member": "m1", "X-Butian-Token": "secret"}, method="POST")
        with urlrequest.urlopen(req, timeout=3) as response:
            payload = json.loads(response.read())
        assert payload["status"] == "ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        service.close()


def test_http_session_switch_denies_other_members_session(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("owner", "Owner")
    service.add_member("other", "Other")
    service.store.set_member_token("other", "other-test-token")
    session_id = service.create_session("solo", current_member_id="owner")["session_id"]
    http_api.ApiHandler.service = service
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urlrequest.Request(f"http://127.0.0.1:{server.server_port}/api/sessions/switch", data=json.dumps({"session_id": session_id, "mode": "team", "current_member_id": "other"}).encode(), headers={"Content-Type": "application/json", "X-Butian-Member": "other", "X-Butian-Token": "other-test-token"}, method="POST")
        with pytest.raises(HTTPError) as denied:
            urlrequest.urlopen(req, timeout=3)
        assert denied.value.code == 403
        assert service.store.session(session_id)["mode"] == "solo"
        assert service.store.session(session_id)["dispatcher_member_id"] == "owner"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        service.close()


def test_mcp_session_switch_denies_other_members_session(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("owner", "Owner")
        service.add_member("other", "Other")
        service.store.set_member_token("other", "other-secret")
        session = service.create_session("solo", current_member_id="owner")
        with pytest.raises(ValueError, match="session_access_denied"):
            mcp_server.call_tool(service, "butian_session_switch_mode", {
                "session_id": session["session_id"], "mode": "team",
                "current_member_id": "other", "actor_member_id": "other", "token": "other-secret",
            })
        unchanged = service.store.session(session["session_id"])
        assert unchanged["mode"] == "solo"
        assert unchanged["dispatcher_member_id"] == "owner"
    finally:
        service.close()


def test_http_header_evidence_is_owned_and_dispatcher_config_requires_admin(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("worker", "Worker")
    service.store.set_member_token("worker", "worker-secret")
    evidence_path = tmp_path / "header-evidence.txt"
    evidence_path.write_text("actual evidence", encoding="utf-8")
    http_api.ApiHandler.service = service
    http_api.ApiHandler.experiments = None
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def post(path, body):
        req = urlrequest.Request(f"http://127.0.0.1:{server.server_address[1]}" + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-Butian-Member": "worker", "X-Butian-Token": "worker-secret"}, method="POST")
        with urlrequest.urlopen(req, timeout=3) as response:
            return json.loads(response.read())
    try:
        recorded = post("/api/evidence", {"path": str(evidence_path)})
        row = service.store.db.execute("SELECT owner_member_id FROM evidence WHERE evidence_id=?", (recorded["data"]["evidence_id"],)).fetchone()
        assert row["owner_member_id"] == "worker"
        with pytest.raises(HTTPError) as forbidden:
            post("/api/sessions/dispatcher", {"member_id": "worker"})
        assert forbidden.value.code == 403
        assert service.get_dispatcher_config() is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        service.close()


@pytest.mark.parametrize("interface", ["http", "mcp"])
def test_foreign_member_cannot_cancel_task_or_answer_question(tmp_path, interface):
    service = ButianService(tmp_path)
    for member in ("owner", "other"):
        service.add_member(member, member)
        service.store.set_member_token(member, member + "-secret")
    session = service.create_session("solo", current_member_id="owner")
    task_id = service.store.create_task(session["session_id"], "private", "owner")
    for revision, status in enumerate(["ready", "claimed", "running"], 1):
        service.store.transition_task(task_id, revision, status, f"private-{revision}")
    question_id = service.ask_user(task_id, 4, "owner", "private scope?")
    server = thread = None
    try:
        if interface == "http":
            http_api.ApiHandler.service = service
            server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            for path, body in [(f"/api/questions/{question_id}/answer", {"answer": "forged"}), (f"/api/tasks/{task_id}/cancel", {"revision": 5})]:
                req = urlrequest.Request(f"http://127.0.0.1:{server.server_address[1]}" + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-Butian-Member": "other", "X-Butian-Token": "other-secret"}, method="POST")
                with pytest.raises(HTTPError) as denied:
                    urlrequest.urlopen(req, timeout=3)
                assert denied.value.code == 403
        else:
            with pytest.raises(ValueError, match="task_access_denied"):
                mcp_server.call_tool(service, "butian_user_answer", {"question_id": question_id, "answer": "forged", "actor_member_id": "other", "token": "other-secret"})
        assert service.store.db.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()["status"] == "waiting_user"
        assert service.store.db.execute("SELECT status FROM questions WHERE question_id=?", (question_id,)).fetchone()["status"] == "open"
    finally:
        if server:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        service.close()


@pytest.mark.parametrize("action,body", [
    ("release", {"lease_id": "foreign-lease"}),
    ("heartbeat", {"lease_id": "foreign-lease"}),
    ("strategy", {"strategy_id": "foreign-strategy"}),
    ("strategy-step", {"revision": 1, "strategy_id": "foreign-strategy", "stage": "plan"}),
])
def test_http_foreign_task_writes_are_denied(tmp_path, action, body):
    service = ButianService(tmp_path)
    for member in ("owner", "other"):
        service.add_member(member, member)
        service.store.set_member_token(member, member + "-secret")
    session = service.create_session("solo", current_member_id="owner")
    task_id = service.store.create_task(session["session_id"], "private", "owner")
    http_api.ApiHandler.service = service
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urlrequest.Request(f"http://127.0.0.1:{server.server_address[1]}/api/tasks/{task_id}/{action}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-Butian-Member": "other", "X-Butian-Token": "other-secret"}, method="POST")
        with pytest.raises(HTTPError) as denied:
            urlrequest.urlopen(req, timeout=3)
        assert denied.value.code == 403
        assert json.loads(denied.value.read())["error"] == "task_access_denied"
        assert service.store.db.execute("SELECT revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()["revision"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        service.close()
