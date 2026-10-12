from __future__ import annotations

from typing import Any, Callable
import json
from ..adapters.base import AgentProvider
from ..adapters.registry import ProviderRegistry
from .store import Store

class ProviderRuntime:
    def __init__(self, store: Store, registry: ProviderRegistry | None = None):
        self.store = store
        self.registry = registry or ProviderRegistry()

    def start(self, member_id: str, prompt: str, *, task_id: str | None = None, provider_id: str | None = None, model: str | None = None) -> dict[str, Any]:
        provider = self.registry.get(provider_id)
        selection = provider.select_model(model)
        if selection.selection_status in {"unsupported", "unknown"}:
            return {"status": selection.selection_status, "provider_id": provider.provider_id, "reason": selection.fallback_reason}
        created = provider.create_child_session(member_id, prompt, selection.effective_model)
        if created.status not in {"accepted", "running", "completed"}:
            return {"status": created.status, "provider_id": provider.provider_id, "reason": created.reason}
        provider_session_id = self.store.create_provider_session(member_id, provider.provider_id, task_id=task_id, model=selection.effective_model, remote_session_id=created.provider_event_id, prompt=prompt)
        return {"status": "running", "provider_session_id": provider_session_id, "provider_id": provider.provider_id, "model": selection.effective_model}

    def consume(self, provider_session_id: str, provider: AgentProvider, prompt: str | None = None, *, tool_executor: Callable[..., dict[str, Any]] | None = None, max_tool_rounds: int = 8, max_tool_calls: int = 32) -> dict[str, Any]:
        if prompt is None:
            prompt = self.store.provider_session(provider_session_id).get("prompt")
            if not prompt:
                raise ValueError("provider_session_prompt_missing")
        output = ""
        final_status: str | None = None
        reason: str | None = None
        session = self.store.provider_session(provider_session_id)
        remote_session_id = session.get("remote_session_id") or provider_session_id
        if max_tool_rounds < 0 or max_tool_calls < 0:
            raise ValueError("invalid_tool_limit")
        seen_calls: set[str] = set()
        tool_rounds = 0
        tool_calls = 0
        message_history = [{"role": "user", "content": prompt}]
        try:
            while final_status is None:
                round_had_tool_call = False
                pending_calls = []
                terminal_event = None
                for event in provider.stream(remote_session_id, prompt):
                    if event.kind == "output":
                        if self.store.record_provider_output(provider_session_id, event.event_id, event.text, event.model):
                            output += event.text
                    elif event.kind == "tool_call":
                        call = event.metadata or {}
                        call_id, tool_id = call.get("call_id"), call.get("tool_id")
                        version, input_data = call.get("version"), call.get("input")
                        if not isinstance(call_id, str) or not isinstance(tool_id, str) or not isinstance(version, int) or not isinstance(input_data, (dict, list, str, int, float, bool, type(None))):
                            final_status, reason = "unknown", "provider_tool_call_invalid"
                            break
                        if call_id in seen_calls:
                            final_status, reason = "unknown", "provider_tool_call_duplicate"
                            pending_calls.clear()
                            break
                        if tool_executor is None:
                            final_status, reason = "unknown", "provider_tool_executor_unavailable"
                            pending_calls.clear()
                            break
                        if tool_calls + len(pending_calls) > max_tool_calls:
                            final_status, reason = "unknown", "provider_tool_call_limit_exceeded"
                            pending_calls.clear()
                            break
                        seen_calls.add(call_id)
                        pending_calls.append((call_id, tool_id, version, input_data))
                        round_had_tool_call = True
                        continue
                    elif event.kind == "status":
                        terminal_event = event
                if pending_calls:
                    if tool_rounds + 1 > max_tool_rounds:
                        final_status, reason = "unknown", "provider_tool_round_limit_exceeded"
                    else:
                        assistant_calls = [{"id": call_id, "type": "function", "function": {"name": tool_id, "arguments": json.dumps(input_data, ensure_ascii=False)}} for call_id, tool_id, version, input_data in pending_calls]
                        message_history.append({"role": "assistant", "tool_calls": assistant_calls})
                        tool_rounds += 1
                        tool_calls += len(pending_calls)
                        for call_id, tool_id, version, input_data in pending_calls:
                            self.store.append_event(
                                provider_session_id,
                                "provider_tool_call",
                                {"call_id": call_id, "tool_id": tool_id, "version": version, "input": input_data, "round": tool_rounds},
                                f"provider-tool-call:{provider_session_id}:{call_id}",
                            )
                            try:
                                try:
                                    result = tool_executor(session["member_id"], tool_id, version, input_data, None)
                                except TypeError:
                                    result = tool_executor(session["member_id"], tool_id, version, input_data)
                            except Exception as exc:
                                self.store.append_event(
                                    provider_session_id,
                                    "provider_tool_result",
                                    {"call_id": call_id, "tool_id": tool_id, "status": "error", "error": type(exc).__name__},
                                    f"provider-tool-result:{provider_session_id}:{call_id}",
                                )
                                final_status, reason = "unknown", f"provider_tool_execution_error:{type(exc).__name__}"
                                break
                            self.store.append_event(
                                provider_session_id,
                                "provider_tool_result",
                                {"call_id": call_id, "tool_id": tool_id, "status": "ok", "result": result},
                                f"provider-tool-result:{provider_session_id}:{call_id}",
                            )
                            message_history.append({"role": "tool", "tool_call_id": call_id, "tool_result": result, "content": json.dumps(result, ensure_ascii=False)})
                        prompt = json.dumps(message_history, ensure_ascii=False)
                    if terminal_event is not None and final_status is None:
                        final_status, reason = self._map_status(terminal_event.status, terminal_event.reason)
                elif terminal_event is not None and final_status is None:
                        final_status, reason = self._map_status(terminal_event.status, terminal_event.reason)
                else:
                    if final_status is None and not round_had_tool_call:
                        final_status, reason = "unknown", "provider_stream_ended_without_terminal_status"
                if final_status is None and tool_rounds == 0:
                    final_status, reason = "unknown", "provider_stream_ended_without_terminal_status"
        except Exception as exc:
            final_status, reason = "unknown", f"provider_stream_error:{type(exc).__name__}"
        if final_status is None:
            final_status, reason = "unknown", "provider_stream_ended_without_terminal_status"
        return self.store.finish_provider_session(provider_session_id, final_status, reason) | {"text": output}

    @staticmethod
    def _map_status(status: str | None, reason: str | None) -> tuple[str, str | None]:
        if status in {"completed", "cancelled", "provider_lost", "unknown", "cancelling"}:
            return status, reason
        if status == "failed":
            return "unknown", reason or "provider_failed_without_recoverability"
        return "unknown", reason or "provider_returned_unknown_status"
