from __future__ import annotations

from typing import Any, Callable
import json
import threading
import os
import hashlib
from ..adapters.base import AgentProvider
from ..adapters.registry import ProviderRegistry
from .store import Store

class ProviderRuntime:
    _consume_guard = threading.Lock()
    _active_consumptions: set[tuple[str, str]] = set()

    def __init__(self, store: Store, registry: ProviderRegistry | None = None):
        self.store = store
        self.registry = registry or ProviderRegistry()

    def start(self, member_id: str, prompt: str, *, task_id: str | None = None, provider_id: str | None = None, model: str | None = None, expected_revision: int | None = None) -> dict[str, Any]:
        provider = self.registry.get(provider_id)
        selection = provider.select_model(model)
        if selection.selection_status in {"unsupported", "unknown"}:
            return {"status": selection.selection_status, "provider_id": provider.provider_id, "reason": selection.fallback_reason}
        with self.store._write_lock:
            task_revision = None
            previous_session = None
            if task_id:
                task = self.store.db.execute("SELECT status,revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if not task or task["status"] != "running" or (expected_revision is not None and task["revision"] != expected_revision):
                    raise ValueError("provider_execution_revision_conflict")
                task_revision = task["revision"]
                previous = self.store.db.execute("SELECT provider_session_id FROM provider_sessions WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
                previous_session = previous[0] if previous else None
        created = provider.create_child_session(member_id, prompt, selection.effective_model)
        if created.status not in {"accepted", "running", "completed"}:
            return {"status": created.status, "provider_id": provider.provider_id, "reason": created.reason}
        conflict = False
        with self.store._write_lock:
            if task_id:
                task = self.store.db.execute("SELECT status,revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                latest = self.store.db.execute("SELECT provider_session_id FROM provider_sessions WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
                conflict = not task or task["status"] != "running" or task["revision"] != task_revision or (latest[0] if latest else None) != previous_session
            if not conflict:
                provider_session_id = self.store.create_provider_session(member_id, provider.provider_id, task_id=task_id, model=selection.effective_model, remote_session_id=created.provider_event_id, prompt=prompt)
        if conflict:
            cancellation = {"status": "unknown", "reason": "remote_session_id_missing"}
            if created.provider_event_id:
                try:
                    result = provider.cancel(created.provider_event_id)
                    cancellation = {"status": result.status, "reason": result.reason}
                except Exception as exc:
                    cancellation = {"status": "unknown", "reason": type(exc).__name__ + ":" + str(exc)}
            self.store.append_event(task_id, "provider_start_superseded", {"provider_id": provider.provider_id, "remote_session_id": created.provider_event_id, "cancellation": cancellation}, f"provider-start-superseded:{task_id}:{task_revision}:{created.provider_event_id}")
            raise ValueError("provider_execution_revision_conflict")
        return {"status": "running", "provider_session_id": provider_session_id, "provider_id": provider.provider_id, "model": selection.effective_model}

    def consume(self, provider_session_id: str, provider: AgentProvider, prompt: str | None = None, *, tool_executor: Callable[..., dict[str, Any]] | None = None, max_tool_rounds: int = 8, max_tool_calls: int = 32) -> dict[str, Any]:
        key = (str(self.store.path.resolve()), provider_session_id)
        with self._consume_guard:
            if key in self._active_consumptions:
                raise ValueError("provider_consume_in_progress")
            self._active_consumptions.add(key)
        lock_file = None
        locked = False
        try:
            lock_dir = self.store.path.parent / ".provider-locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock_path = lock_dir / (hashlib.sha256(repr(key).encode()).hexdigest() + ".lock")
            lock_file = open(lock_path, "a+b")
            if lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError("provider_consume_in_progress") from exc
            locked = True
            return self._consume(provider_session_id, provider, prompt, tool_executor=tool_executor, max_tool_rounds=max_tool_rounds, max_tool_calls=max_tool_calls)
        finally:
            try:
                if lock_file is not None:
                    try:
                        if locked:
                            lock_file.seek(0)
                            if os.name == "nt":
                                import msvcrt
                                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                            else:
                                import fcntl
                                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    finally:
                        lock_file.close()
            finally:
                with self._consume_guard:
                    self._active_consumptions.discard(key)

    def _assert_stream_current(self, provider_session_id: str) -> None:
        session = self.store.provider_session(provider_session_id)
        if session["status"] != "running":
            raise ValueError("provider_session_terminal")
        if session.get("task_id"):
            task = self.store.db.execute("SELECT status,revision FROM tasks WHERE task_id=?", (session["task_id"],)).fetchone()
            latest = self.store.db.execute("SELECT provider_session_id FROM provider_sessions WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (session["task_id"],)).fetchone()
            if not task or task["status"] != "running" or task["revision"] != session["task_revision"]:
                raise ValueError("provider_execution_revision_conflict")
            if not latest or latest[0] != provider_session_id:
                raise ValueError("provider_execution_superseded")

    def _consume(self, provider_session_id: str, provider: AgentProvider, prompt: str | None = None, *, tool_executor: Callable[..., dict[str, Any]] | None = None, max_tool_rounds: int = 8, max_tool_calls: int = 32) -> dict[str, Any]:
        persisted = self.store.provider_session(provider_session_id)
        if persisted["status"] in {"completed", "cancelled", "provider_lost", "unknown"}:
            rows = self.store.db.execute("SELECT text FROM provider_outputs WHERE provider_session_id=? ORDER BY created_at,provider_event_id", (provider_session_id,)).fetchall()
            return persisted | {"text": "".join(row["text"] for row in rows)}
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
                batch_invalid = False
                for event in provider.stream(remote_session_id, prompt):
                    with self.store._write_lock:
                        self._assert_stream_current(provider_session_id)
                        if event.kind == "output":
                            if self.store.record_provider_output(provider_session_id, event.event_id, event.text, event.model):
                                output += event.text
                    if event.kind == "output":
                        continue
                    elif event.kind == "tool_call":
                        call = event.metadata or {}
                        call_id, tool_id = call.get("call_id"), call.get("tool_id")
                        version, input_data = call.get("version"), call.get("input")
                        if (not isinstance(call_id, str) or not call_id.strip() or
                                not isinstance(tool_id, str) or not tool_id.strip() or
                                isinstance(version, bool) or not isinstance(version, int) or version < 1 or
                                not isinstance(input_data, (dict, list, str, int, float, bool, type(None)))):
                            final_status, reason = "unknown", "provider_tool_call_invalid"
                            pending_calls.clear()
                            batch_invalid = True
                            break
                        if call_id in seen_calls:
                            final_status, reason = "unknown", "provider_tool_call_duplicate"
                            pending_calls.clear()
                            break
                        if tool_executor is None:
                            final_status, reason = "unknown", "provider_tool_executor_unavailable"
                            pending_calls.clear()
                            break
                        if tool_calls + len(pending_calls) + 1 > max_tool_calls:
                            final_status, reason = "unknown", "provider_tool_call_limit_exceeded"
                            pending_calls.clear()
                            break
                        seen_calls.add(call_id)
                        share_id = call.get("share_id")
                        if share_id is not None and (not isinstance(share_id, str) or not share_id):
                            final_status, reason = "unknown", "provider_tool_call_invalid"
                            pending_calls.clear()
                            batch_invalid = True
                            break
                        pending_calls.append((call_id, tool_id, version, input_data, share_id))
                        round_had_tool_call = True
                        continue
                    elif event.kind == "status":
                        terminal_event = event
                if batch_invalid:
                    pending_calls.clear()
                if pending_calls and terminal_event is not None and terminal_event.status not in {None, "completed"}:
                    final_status, reason = self._map_status(terminal_event.status, terminal_event.reason)
                    pending_calls.clear()
                if pending_calls:
                    if tool_rounds + 1 > max_tool_rounds:
                        final_status, reason = "unknown", "provider_tool_round_limit_exceeded"
                    else:
                        assistant_calls = [{"id": call_id, "type": "function", "function": {"name": "butian_tool_execute", "arguments": json.dumps({"tool_id": tool_id, "version": version, "input": input_data}, ensure_ascii=False)}} for call_id, tool_id, version, input_data, share_id in pending_calls]
                        message_history.append({"role": "assistant", "tool_calls": assistant_calls})
                        tool_rounds += 1
                        tool_calls += len(pending_calls)
                        for call_id, tool_id, version, input_data, share_id in pending_calls:
                            self.store.append_event(
                                provider_session_id,
                                "provider_tool_call",
                                {"call_id": call_id, "tool_id": tool_id, "version": version, "input": input_data, "round": tool_rounds},
                                f"provider-tool-call:{provider_session_id}:{call_id}",
                            )
                            try:
                                result = tool_executor(session["member_id"], tool_id, version, input_data, share_id)
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
        with self.store._write_lock:
            current = self.store.provider_session(provider_session_id)
            if current["status"] in {"completed", "cancelled", "provider_lost", "unknown"}:
                return current | {"text": output}
            if final_status == "completed":
                try:
                    self._assert_stream_current(provider_session_id)
                except ValueError as exc:
                    final_status, reason = "unknown", str(exc)
            if current["status"] == "cancelling" and final_status == "completed":
                final_status, reason = "unknown", "provider_completed_during_cancellation"
            return self.store.finish_provider_session(provider_session_id, final_status, reason) | {"text": output}

    @staticmethod
    def _map_status(status: str | None, reason: str | None) -> tuple[str, str | None]:
        if status in {"completed", "cancelled", "provider_lost", "unknown", "cancelling"}:
            return status, reason
        if status == "failed":
            return "unknown", reason or "provider_failed_without_recoverability"
        return "unknown", reason or "provider_returned_unknown_status"
