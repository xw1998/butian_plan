import sys
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from butian.adapters.mcp import McpProvider
from butian.adapters.config import build_registry


def test_mcp_context_window_is_explicit():
    assert McpProvider([sys.executable, "-c", "pass"]).capabilities().context_window is None
    registry, errors = build_registry([{"type": "mcp", "command": [sys.executable, "-c", "pass"], "context_window": 32768}])
    assert not errors
    assert registry.get("mcp").capabilities().context_window == 32768


def test_mcp_silent_process_times_out_and_is_reaped(tmp_path, monkeypatch):
    processes = []
    original = subprocess.Popen
    def tracked(*args, **kwargs):
        proc = original(*args, **kwargs)
        processes.append(proc)
        return proc
    monkeypatch.setattr(subprocess, "Popen", tracked)
    provider = McpProvider([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.2)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(lambda: list(provider.stream("silent", "task")))
    try:
        events = future.result(timeout=3)
        assert events[-1].status == "unknown"
        assert events[-1].reason == "mcp_response_timeout"
        assert processes and all(proc.poll() is not None for proc in processes)
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
        pool.shutdown(wait=True)


def test_real_stdio_host_without_model_provider_and_restart(tmp_path):
    import os
    import json
    import queue
    import threading
    from pathlib import Path
    from butian.runtime.service import ButianService
    home = tmp_path / "host-home"
    service = ButianService(home)
    service.add_member("host-member", "Host member")
    service.store.set_member_token("host-member", "transport-test-token")
    service.close()
    assert not (home / "providers.json").exists()
    auth = {"member_id": "host-member", "actor_member_id": "host-member", "token": "transport-test-token"}
    env = dict(os.environ, BUTIAN_HOME=str(home))
    env.pop("BUTIAN_PROVIDERS_FILE", None)
    def exercise(restart=False):
        proc = subprocess.Popen([sys.executable, "-m", "butian.cli", "mcp"], cwd=Path(__file__).resolve().parents[1], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        responses = queue.Queue()
        reader = threading.Thread(target=lambda: [responses.put(line) for line in proc.stdout], daemon=True)
        reader.start()
        def request(method, params):
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            response = json.loads(responses.get(timeout=5))
            assert "error" not in response, response
            return response["result"]
        def call(name, **args):
            return request("tools/call", {"name": name, "arguments": dict(auth, **args)})
        try:
            assert request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "generic-host-test", "version": "1"}})["capabilities"]["tools"] == {}
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()
            tools = request("tools/list", {})["tools"]
            assert any(t["name"] == "butian_memory_recall" for t in tools)
            if restart:
                memories = call("butian_memory_search", query="calibration")["memories"]
                assert any(m["memory_id"] == "host-memory" for m in memories)
                assert any(t["status"] == "done" for t in call("butian_task_list")["tasks"])
                return
            session = call("butian_session_create", mode="solo", current_member_id="host-member")
            task = call("butian_task_create", session_id=session["session_id"], title="Host calibration", assignee_id="host-member")
            current = next(t for t in call("butian_task_list")["tasks"] if t["task_id"] == task["task_id"])
            call("butian_task_transition", task_id=task["task_id"], expected_revision=current["revision"], target="ready", operation_key="host-ready")
            def current_task():
                return next(t for t in call("butian_task_list")["tasks"] if t["task_id"] == task["task_id"])
            call("butian_task_claim", task_id=task["task_id"])
            call("butian_task_transition", task_id=task["task_id"], expected_revision=current_task()["revision"], target="running", operation_key="host-running")
            composed = call("butian_context_compose", instruction="perform calibration", task_id=task["task_id"])
            assert "context" in composed and "report" in composed
            evidence_file = home / "calibration.txt"
            evidence_file.write_text("Measured calibration result: 12.5", encoding="utf-8")
            evidence_id = call("butian_evidence_record", path=str(evidence_file))["evidence_id"]
            call("butian_task_transition", task_id=task["task_id"], expected_revision=current_task()["revision"], target="review", operation_key="host-review")
            call("butian_memory_practice", memory_id="host-method", kind="lesson", content="calibration needs evidence", outcome="unknown", independent_key="seed-method")
            call("butian_task_handoff", task_id=task["task_id"], task_revision=current_task()["revision"], status="completed", summary="Calibration measured", evidence_ids=[evidence_id], next_action="Review measurement", confidence=0.9, method_uses=[{"memory_id": "host-method", "outcome": "success", "evidence_id": evidence_id}])
            call("butian_task_review", task_id=task["task_id"], revision=current_task()["revision"], verdict="approve", reviewer_member_id="host-member")
            assert current_task()["status"] == "done"
            check = ButianService(home)
            try:
                memory = check.memory("host-member")
                try:
                    assert memory.db.execute("SELECT COUNT(*) FROM practice_observations WHERE memory_id='host-method' AND outcome='success'").fetchone()[0] == 1
                    assert memory.db.execute("SELECT 1 FROM skill_states WHERE skill_domain='practice:lesson'").fetchone()
                finally:
                    memory.close()
            finally:
                check.close()
            call("butian_memory_practice", memory_id="host-memory", kind="experience", content="calibration requires measured evidence", outcome="unknown", independent_key="host-observation")
            assert call("butian_memory_search", query="calibration")["memories"]
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            reader.join(timeout=2)
            proc.stdout.close()
        assert proc.returncode == 0
    exercise()
    exercise(restart=True)


def test_host_completion_tools_are_discoverable():
    from butian.mcp_server import TOOLS
    names = {tool["name"] for tool in TOOLS}
    assert {"butian_context_compose", "butian_evidence_record", "butian_task_handoff"} <= names


def test_host_handoff_rejects_other_assignee_and_stale_revision(tmp_path):
    import pytest
    from butian.runtime.service import ButianService
    from butian.mcp_server import call_tool
    service = ButianService(tmp_path)
    try:
        for member in ("owner", "other"):
            service.add_member(member, member)
            service.store.set_member_token(member, "test-token")
        session = service.create_session("solo", current_member_id="owner")
        task_id = service.store.create_task(session["session_id"], "owned", "owner")
        auth = {"member_id": "other", "actor_member_id": "other", "token": "test-token"}
        with pytest.raises(ValueError):
            call_tool(service, "butian_context_compose", dict(auth, task_id=task_id, instruction="read private task"))
        with pytest.raises(ValueError):
            call_tool(service, "butian_task_handoff", dict(auth, task_id=task_id, task_revision=1, status="completed", summary="unauthorized", evidence_ids=[], next_action="review", confidence=1))
        service.store.transition_task(task_id, 1, "ready", "ready")
        lease = service.store.claim_task(task_id, "owner", internal=True)
        service.store.transition_task(task_id, lease["revision"], "running", "running")
        auth = {"member_id": "owner", "actor_member_id": "owner", "token": "test-token"}
        with pytest.raises(ValueError, match="handoff_stale_revision"):
            call_tool(service, "butian_task_handoff", dict(auth, task_id=task_id, task_revision=1, status="completed", summary="stale", evidence_ids=[], next_action="review", confidence=1))
    finally:
        service.close()


def test_mcp_server_recovers_after_malformed_first_line(tmp_path, monkeypatch, capsys):
    import io
    import json
    from butian import mcp_server
    monkeypatch.setattr(sys, "stdin", io.StringIO('not-json\n' + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}}) + '\n'))
    mcp_server.run(tmp_path)
    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["id"] == 2 and "result" in responses[1]
