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
