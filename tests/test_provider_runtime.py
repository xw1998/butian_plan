import json
import sys

from butian.adapters.local import LocalCommandProvider
from butian.adapters.registry import ProviderRegistry
from butian.runtime.provider_runtime import ProviderRuntime
from butian.runtime.store import Store


class ScriptedProvider:
    provider_id = "scripted"

    def capabilities(self):
        from butian.core.models import ProviderCapabilities
        return ProviderCapabilities(can_create_child_session=True, can_stream_output=True)

    def models(self):
        return ["test-model"]

    def select_model(self, requested_model):
        from butian.core.models import ModelSelection
        return ModelSelection(requested_model, requested_model or "test-model", "applied")

    def create_child_session(self, member_id, prompt, model=None):
        from butian.adapters.base import ProviderResult
        return ProviderResult("accepted", provider_event_id="session-1", model=model)

    def stream(self, provider_session_id, prompt):
        from butian.adapters.base import ProviderEvent
        yield ProviderEvent("chunk-1", "output", "hello ", "test-model")
        yield ProviderEvent("chunk-1", "output", "duplicate", "test-model")
        yield ProviderEvent("finish", "status", status="completed")

    def cancel(self, provider_session_id):
        from butian.adapters.base import ProviderResult
        return ProviderResult("unsupported", reason="not supported")


def test_provider_runtime_deduplicates_output_and_records_actual_model(tmp_path):
    store = Store(tmp_path / "team.db")
    registry = ProviderRegistry()
    provider = ScriptedProvider()
    registry.register(provider)
    runtime = ProviderRuntime(store, registry)
    started = runtime.start("member", "prompt", provider_id="scripted", model="test-model")
    result = runtime.consume(started["provider_session_id"], provider, "prompt")
    assert result["status"] == "completed"
    assert result["text"] == "hello "
    assert result["model"] == "test-model"
    assert store.db.execute("SELECT COUNT(*) FROM provider_outputs").fetchone()[0] == 1
    store.close()


def test_provider_without_terminal_event_is_unknown(tmp_path):
    store = Store(tmp_path / "team.db")

    class LostProvider(ScriptedProvider):
        provider_id = "lost"
        def stream(self, provider_session_id, prompt):
            from butian.adapters.base import ProviderEvent
            yield ProviderEvent("part", "output", "partial")

    provider = LostProvider()
    session = store.create_provider_session("m", provider.provider_id)
    result = ProviderRuntime(store).consume(session, provider, "prompt")
    assert result["status"] == "unknown"
    assert result["reason"] == "provider_stream_ended_without_terminal_status"
    store.close()


def test_unknown_provider_is_explicitly_unsupported(tmp_path):
    runtime = ProviderRuntime(Store(tmp_path / "team.db"))
    result = runtime.start("m", "prompt", provider_id="missing")
    assert result["status"] == "unsupported"


def test_local_command_provider_executes_prompt(tmp_path):
    store = Store(tmp_path / "team.db")
    provider = LocalCommandProvider([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"])
    registry = ProviderRegistry(); registry.register(provider)
    runtime = ProviderRuntime(store, registry)
    session = runtime.start("m", "hello", provider_id="local-command")
    result = runtime.consume(session["provider_session_id"], provider, "hello")
    assert result["status"] == "completed"
    assert result["text"].strip() == "HELLO"
    store.close()


def test_remote_provider_session_id_is_passed_to_stream(tmp_path):
    store = Store(tmp_path / "team.db")

    class RemoteProvider(ScriptedProvider):
        def stream(self, provider_session_id, prompt):
            assert provider_session_id == "session-1"
            yield from super().stream(provider_session_id, prompt)

    provider = RemoteProvider()
    registry = ProviderRegistry(); registry.register(provider)
    runtime = ProviderRuntime(store, registry)
    started = runtime.start("member", "prompt", provider_id="scripted")
    session = store.provider_session(started["provider_session_id"])
    assert session["provider_session_id"] != session["remote_session_id"]
    result = runtime.consume(started["provider_session_id"], provider, "prompt")
    assert result["status"] == "completed"
    store.close()


def test_mcp_provider_discovers_and_calls_schema_compatible_tool():
    from butian.adapters.mcp import McpProvider
    code = (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line); m=r.get('method'); i=r.get('id')\n"
        " if r.get('method')=='initialize': open(sys.argv[1],'a').write('init\\n')\n"
        " if i is None: continue\n"
        " if m=='tools/list': out={'tools':[{'name':'execute_task','description':'Run an agent task','inputSchema':{'type':'object','required':['task'],'properties':{'task':{'type':'string'}}}}]}\n"
        " elif m=='tools/call': out={'content':[{'type':'text','text':'done:'+r['params']['arguments']['task']}] }\n"
        " else: out={}\n"
        " print(json.dumps({'jsonrpc':'2.0','id':i,'result':out}),flush=True)\n"
    )
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as directory:
        marker = Path(directory) / "starts"
        events = list(McpProvider([sys.executable, "-c", code, str(marker)]).stream("local-session", "do work"))
        assert marker.read_text().splitlines() == ["init"]
    assert events[0].kind == "output" and events[0].text == "done:do work"
    assert events[-1].status == "completed"


def test_mcp_model_selection_does_not_claim_unapplied_model():
    from butian.adapters.mcp import McpProvider
    result = McpProvider([sys.executable, "-c", "pass"]).select_model("model-x")
    assert result.selection_status == "unknown"
    assert result.effective_model is None

def test_mcp_provider_reports_unknown_when_no_prompt_tool_is_discoverable():
    from butian.adapters.mcp import McpProvider
    code = (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line); i=r.get('id')\n"
        " if i is not None: print(json.dumps({'jsonrpc':'2.0','id':i,'result':{'tools':[{'name':'status','inputSchema':{'type':'object','properties':{}}}]}} if r.get('method')=='tools/list' else {'jsonrpc':'2.0','id':i,'result':{}}),flush=True)\n"
    )
    events = list(McpProvider([sys.executable, "-c", code]).stream("local-session", "do work"))
    assert events[-1].status == "unknown"
    assert events[-1].reason == "mcp_no_unambiguous_prompt_tool"


def test_completed_empty_output_remains_unknown_for_team_child(tmp_path):
    from butian.adapters.base import ProviderEvent, ProviderResult
    from butian.runtime.service import ButianService

    class EmptyProvider(ScriptedProvider):
        provider_id = "empty"
        def stream(self, provider_session_id, prompt):
            yield ProviderEvent("done", "status", status="completed")

    home = tmp_path / "empty-team"
    service = ButianService(home)
    try:
        service.providers.register(EmptyProvider())
        service.add_member("dispatcher", "Dispatcher")
        service.add_member("worker", "Worker", provider_id="empty", model_id="test-model")
        goal = service.submit_goal("goal", mode="team", dispatcher_member_id="dispatcher", member_ids=["worker"], subtasks=["do work"])
        result = service.run_team_goal(goal["task_id"])
        child = service.store.db.execute("SELECT status FROM tasks WHERE parent_id=?", (goal["task_id"],)).fetchone()
        assert result["children"][0]["status"] == "unknown"
        assert child["status"] == "unknown"
    finally:
        service.close()

def test_failed_provider_status_maps_to_unknown_with_reason(tmp_path):
    from butian.adapters.base import ProviderEvent
    store = Store(tmp_path / "failed.db")
    class FailedProvider(ScriptedProvider):
        def stream(self, provider_session_id, prompt):
            yield ProviderEvent("failure", "status", status="failed", reason="provider_error_503")
    provider = FailedProvider()
    session_id = store.create_provider_session("member", provider.provider_id, prompt="prompt")
    result = ProviderRuntime(store).consume(session_id, provider)
    assert result["status"] == "unknown"
    assert result["reason"] == "provider_error_503"
    assert store.provider_session(session_id)["status"] == "unknown"
    store.close()

def test_provider_lost_status_is_preserved(tmp_path):
    from butian.adapters.base import ProviderEvent
    store = Store(tmp_path / "lost.db")
    class LostProvider(ScriptedProvider):
        def stream(self, provider_session_id, prompt):
            yield ProviderEvent("lost", "status", status="provider_lost", reason="remote_session_missing")
    provider = LostProvider()
    session_id = store.create_provider_session("member", provider.provider_id, prompt="prompt")
    result = ProviderRuntime(store).consume(session_id, provider)
    assert result["status"] == "provider_lost"
    assert result["reason"] == "remote_session_missing"
    store.close()


def test_provider_runtime_executes_authorized_tool_and_returns_result(tmp_path):
    from butian.adapters.base import ProviderEvent
    store = Store(tmp_path / "tool-loop.db")
    calls = []

    class ToolProvider(ScriptedProvider):
        provider_id = "tool-provider"
        def __init__(self):
            self.round = 0
        def stream(self, provider_session_id, prompt):
            self.round += 1
            if self.round == 1:
                yield ProviderEvent("call-1", "tool_call", metadata={"call_id": "call-1", "tool_id": "t1", "version": 1, "input": {"x": 1}})
            else:
                assert '"tool_result"' in prompt
                yield ProviderEvent("out-1", "output", "done")
                yield ProviderEvent("finish", "status", status="completed")

    provider = ToolProvider()
    session_id = store.create_provider_session("member-1", provider.provider_id, remote_session_id="remote-1", prompt="start")
    result = ProviderRuntime(store).consume(
        session_id, provider,
        tool_executor=lambda member, tool, version, data, share_id=None: calls.append((member, tool, version, data, share_id)) or {"ok": True},
    )
    assert result["status"] == "completed"
    assert result["text"] == "done"
    assert calls == [("member-1", "t1", 1, {"x": 1}, None)]
    audit = store.db.execute("SELECT event_type, payload FROM events WHERE aggregate_id=? ORDER BY seq", (session_id,)).fetchall()
    assert [row["event_type"] for row in audit] == ["provider_tool_call", "provider_tool_result"]
    assert json.loads(audit[0]["payload"])["call_id"] == "call-1"
    assert json.loads(audit[1]["payload"])["result"] == {"ok": True}
    store.close()


def test_openai_compatible_provider_emits_tool_call_event(monkeypatch):
    from butian.adapters.http import OpenAICompatibleProvider

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def read(self):
            return (b'{"model":"gpt-test","choices":[{"message":{"role":"assistant","content":null,"tool_calls":[{"id":"call-1","type":"function","function":{"name":"butian_tool_execute","arguments":"{\\"tool_id\\":\\"tool-1\\",\\"version\\":7,\\"input\\":{\\"x\\":1}}"}}]}}]}')

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())
    provider = OpenAICompatibleProvider("http://provider", model="gpt-test")
    session = provider.create_child_session("member", "prompt").provider_event_id
    provider.set_session_tools(session, [{"tool_id": "tool-1", "version": 7, "permission": "owner_execute", "description": "demo", "input_contract": {"type": "object", "properties": {"x": {}}, "required": ["x"]}}])
    events = list(provider.stream(session, "prompt"))
    assert events[0].kind == "tool_call"
    assert events[0].metadata == {"call_id": "call-1", "tool_id": "tool-1", "version": 7, "input": {"x": 1}, "share_id": None}


def test_openai_compatible_provider_resolves_generic_tool_envelope(monkeypatch):
    from butian.adapters.http import OpenAICompatibleProvider

    class Response:
        def __enter__(self): return self
        def __exit__(self, exc_type, exc, tb): return False
        def read(self):
            return b'{"model":"gpt-test","choices":[{"message":{"content":null,"tool_calls":[{"id":"call-1","type":"function","function":{"name":"butian_tool_execute","arguments":"{\\"tool_id\\":\\"tool-1\\",\\"version\\":7,\\"input\\":{\\"x\\":1}}"}}]}}]}'

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())
    provider = OpenAICompatibleProvider("http://provider", model="gpt-test")
    session = provider.create_child_session("member", "prompt").provider_event_id
    provider.set_session_tools(session, [{"tool_id": "tool-1", "version": 7, "permission": "team_execute", "share_id": "share-1"}])
    event = list(provider.stream(session, "prompt"))[0]
    assert event.kind == "tool_call"
    assert event.metadata == {"call_id": "call-1", "tool_id": "tool-1", "version": 7, "input": {"x": 1}, "share_id": "share-1"}


def test_openai_compatible_provider_rejects_malformed_tool_arguments(monkeypatch):
    from butian.adapters.http import OpenAICompatibleProvider

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def read(self):
            return (b'{"choices":[{"message":{"content":null,"tool_calls":[{"id":"call-1","type":"function","function":{"name":"butian_tool_execute","arguments":"not-json"}}]}}]}')

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())
    provider = OpenAICompatibleProvider("http://provider", model="gpt-test")
    session = provider.create_child_session("member", "prompt").provider_event_id
    provider.set_session_tools(session, [{"tool_id": "tool-1", "version": 1, "permission": "owner_execute"}])
    events = list(provider.stream(session, "prompt"))
    assert events[-1].kind == "status"
    assert events[-1].status == "unknown"
    assert events[-1].reason == "provider_tool_call_invalid_arguments"


def test_invalid_tool_batch_has_no_side_effects(tmp_path):
    from butian.adapters.base import ProviderEvent
    store = Store(tmp_path / "invalid-batch.db")
    class InvalidBatch(ScriptedProvider):
        def stream(self, provider_session_id, prompt):
            yield ProviderEvent("call", "tool_call", metadata={"call_id": "one", "tool_id": "t1", "version": 1, "input": {}})
            yield ProviderEvent("bad", "tool_call", metadata={})
    calls = []
    session_id = store.create_provider_session("member", "invalid", prompt="prompt")
    result = ProviderRuntime(store).consume(session_id, InvalidBatch(), tool_executor=lambda *args: calls.append(args) or {})
    assert result["status"] == "unknown"
    assert calls == []
    store.close()


def test_tool_call_budget_is_inclusive(tmp_path):
    from butian.adapters.base import ProviderEvent
    store = Store(tmp_path / "budget.db")
    class OneCall(ScriptedProvider):
        def stream(self, provider_session_id, prompt):
            yield ProviderEvent("call", "tool_call", metadata={"call_id": "one", "tool_id": "t1", "version": 1, "input": {}})
    for limit, expected in ((0, 0), (1, 1)):
        calls = []
        session_id = store.create_provider_session("member", "budget", prompt="prompt")
        ProviderRuntime(store).consume(session_id, OneCall(), max_tool_calls=limit, tool_executor=lambda *args: calls.append(args) or {})
        assert len(calls) == expected
    store.close()


def test_executor_typeerror_is_not_retried_with_legacy_signature(tmp_path):
    from butian.adapters.base import ProviderEvent
    store = Store(tmp_path / "executor-error.db")
    class OneCall(ScriptedProvider):
        def stream(self, provider_session_id, prompt):
            yield ProviderEvent("call", "tool_call", metadata={"call_id": "one", "tool_id": "t1", "version": 1, "input": {}})
    calls = []
    def executor(*args):
        calls.append(args)
        raise TypeError("executor body failure")
    session_id = store.create_provider_session("member", "executor-error", prompt="prompt")
    result = ProviderRuntime(store).consume(session_id, OneCall(), tool_executor=executor)
    assert result["status"] == "unknown"
    assert len(calls) == 1
    store.close()


def test_openai_provider_empty_tools_and_session_bound_tool_flow(monkeypatch, tmp_path):
    from butian.adapters.http import OpenAICompatibleProvider
    from butian.runtime.service import ButianService

    provider = OpenAICompatibleProvider("http://provider", model="gpt-test")
    service = ButianService(tmp_path / "home")
    try:
        service.providers.register(provider)
        member = service.store.db.execute("SELECT member_id FROM members ORDER BY member_id LIMIT 1").fetchone()[0]
        prompt = "unrelated prompt"
        started = service.provider_runtime.start(member, prompt, provider_id=provider.provider_id, model="gpt-test")
        local_id = started["provider_session_id"]
        remote_id = service.store.provider_session(local_id)["remote_session_id"]
        captured = []

        class Response:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return False
            def read(self): return self.body

        def fake_urlopen(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            captured.append(body)
            if not body["tools"]:
                return Response(b'{"model":"gpt-test","choices":[{"message":{"content":"no tools"}}]}')
            if not any(message.get("role") == "tool" for message in body["messages"]):
                return Response(b'{"model":"gpt-test","choices":[{"message":{"content":null,"tool_calls":[{"id":"call-1","type":"function","function":{"name":"butian_tool_execute","arguments":"{\\"tool_id\\":\\"tool-1\\",\\"version\\":7,\\"input\\":{\\"x\\":1}}"}}]}}]}')
            return Response(b'{"model":"gpt-test","choices":[{"message":{"content":"done"}}]}')

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        empty = list(provider.stream(remote_id, "unrelated prompt"))
        assert empty[-1].status == "completed"
        assert captured[-1]["tools"] == []
        captured.clear()

        executed = []
        service.tools.context_cards = lambda member_id, instruction, limit=32: [{"tool_id": "tool-1", "name": "demo", "description": "demo", "version": 7, "permission": "team_execute", "share_id": "share-server", "input_contract": {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}}]
        service.execute_tool = lambda member_id, tool_id, version, data, **kwargs: executed.append((member_id, tool_id, version, data, kwargs.get("share_id"))) or {"ok": True}
        result = service.consume_provider_task(local_id)
        assert result["status"] == "completed"
        assert result["text"] == "done"
        assert captured[0]["tools"][0]["function"]["name"] == "butian_tool_execute"
        assert executed == [(member, "tool-1", 7, {"x": 1}, "share-server")]
        assert any(message.get("role") == "tool" for message in captured[1]["messages"])
    finally:
        service.close()


def test_openai_provider_rejects_unbound_or_version_tampered_tool(monkeypatch):
    from butian.adapters.http import OpenAICompatibleProvider

    class Response:
        def __enter__(self): return self
        def __exit__(self, exc_type, exc, tb): return False
        def read(self):
            return b'{"choices":[{"message":{"content":null,"tool_calls":[{"id":"call-1","type":"function","function":{"name":"butian_tool_execute","arguments":"{\\"tool_id\\":\\"tool-1\\",\\"version\\":8,\\"input\\":{}}"}}]}}]}'

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())
    provider = OpenAICompatibleProvider("http://provider", model="gpt-test")
    session = provider.create_child_session("member", "prompt").provider_event_id
    provider.set_session_tools(session, [
        {"tool_id": "tool-1", "version": 7, "permission": "team_execute", "share_id": "share-1"},
        {"tool_id": "readonly-tool", "version": 1, "permission": "team_readonly"},
    ])
    events = list(provider.stream(session, "prompt"))
    assert events[-1].kind == "status"
    assert events[-1].reason == "provider_tool_not_authorized"
    assert len(provider._session_tools[session]) == 1


def test_provider_runtime_sends_standard_tool_message_and_bound_share(tmp_path):
    from butian.adapters.base import ProviderEvent
    store = Store(tmp_path / "generic-tool.db")
    calls = []

    class GenericProvider(ScriptedProvider):
        provider_id = "generic"
        def stream(self, provider_session_id, prompt):
            if prompt == "initial":
                yield ProviderEvent("call", "tool_call", metadata={"call_id": "call-1", "tool_id": "tool-1", "version": 7, "input": {"x": 1}, "share_id": "share-1"})
            else:
                history = json.loads(prompt)
                assert history[1]["tool_calls"][0]["function"]["name"] == "butian_tool_execute"
                assert json.loads(history[1]["tool_calls"][0]["function"]["arguments"]) == {"tool_id": "tool-1", "version": 7, "input": {"x": 1}}
                assert history[2]["role"] == "tool" and history[2]["tool_call_id"] == "call-1"
                yield ProviderEvent("done", "output", text="done")
                yield ProviderEvent("finish", "status", status="completed")

    provider = GenericProvider()
    session = store.create_provider_session("member", "generic", remote_session_id="remote-1", prompt="initial")
    result = ProviderRuntime(store).consume(session, provider, "initial", tool_executor=lambda *args: calls.append(args) or {"ok": True})
    assert result["status"] == "completed"
    assert calls == [("member", "tool-1", 7, {"x": 1}, "share-1")]
    store.close()


def test_provider_tool_call_identifiers_and_version_are_fail_closed(tmp_path):
    from butian.adapters.base import ProviderEvent
    store = Store(tmp_path / "invalid-tool-call.db")

    class InvalidCalls(ScriptedProvider):
        def __init__(self, metadata):
            self.metadata = metadata

        def stream(self, provider_session_id, prompt):
            yield ProviderEvent("event", "tool_call", metadata=self.metadata)

    cases = [
        {"call_id": "", "tool_id": "t1", "version": 1, "input": {}},
        {"call_id": "c1", "tool_id": "", "version": 1, "input": {}},
        {"call_id": "c1", "tool_id": "t1", "version": True, "input": {}},
        {"call_id": "c1", "tool_id": "t1", "version": 0, "input": {}},
        {"call_id": "c1", "tool_id": "t1", "version": -1, "input": {}},
    ]
    try:
        for index, metadata in enumerate(cases):
            session_id = store.create_provider_session(f"member-{index}", "scripted", prompt="prompt")
            result = ProviderRuntime(store).consume(session_id, InvalidCalls(metadata), tool_executor=lambda *args: {})
            assert result["status"] == "unknown"
            assert result["reason"] == "provider_tool_call_invalid"
    finally:
        store.close()
