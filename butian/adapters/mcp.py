from __future__ import annotations

import json
import subprocess
import uuid
from typing import Any

from ..core.models import ModelSelection, ProviderCapabilities
from .base import ProviderEvent, ProviderResult


class McpProvider:
    """One-process JSON-RPC bridge for stateless child sessions."""

    provider_id = "mcp"

    def __init__(self, command: list[str], *, timeout: float = 30):
        if not command:
            raise ValueError("command_required")
        self.command = list(command)
        self.timeout = timeout

    def capabilities(self):
        return ProviderCapabilities(can_create_child_session=True, can_stream_output=False)

    def models(self):
        return []

    def select_model(self, requested_model):
        if requested_model is None:
            return ModelSelection(None, None, "applied")
        return ModelSelection(requested_model, None, "unknown", "MCP tool does not expose model selection")

    def create_child_session(self, member_id, prompt, model=None):
        return ProviderResult("accepted", provider_event_id=f"mcp_{uuid.uuid4().hex}", model=None)

    @staticmethod
    def _select_tool(tools: list[dict[str, Any]]) -> tuple[dict[str, Any], str] | None:
        candidates = []
        for tool in tools:
            name = tool.get("name")
            schema = tool.get("inputSchema")
            properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
            if not isinstance(name, str) or not isinstance(properties, dict):
                continue
            fields = [field for field in ("prompt", "task", "message", "input", "request", "query") if isinstance(properties.get(field), dict) and properties[field].get("type") == "string"]
            if not fields:
                continue
            normalized = name.lower()
            score = sum(token in normalized for token in ("agent", "run", "execute", "dispatch", "chat", "ask", "complete"))
            description = tool.get("description", "")
            if isinstance(description, str):
                desc = description.lower()
                score += sum(token in desc for token in ("agent", "execute", "run", "task", "prompt"))
            required = schema.get("required", [])
            field = next((item for item in fields if item in required), fields[0])
            candidates.append((score, tool, field))
        candidates.sort(key=lambda item: -item[0])
        if not candidates or candidates[0][0] == 0 or (len(candidates) > 1 and candidates[1][0] == candidates[0][0]):
            return None
        return candidates[0][1], candidates[0][2]

    def _read_response(self, proc: subprocess.Popen[str], request_id: str) -> dict[str, Any]:
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("mcp_server_closed_stdout")
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(response, dict) and str(response.get("id")) == request_id:
                return response

    def _request(self, proc: subprocess.Popen[str], request: dict[str, Any]) -> dict[str, Any]:
        assert proc.stdin is not None
        request_id = str(request["id"])
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        response = self._read_response(proc, request_id)
        if "error" in response:
            raise RuntimeError("mcp_jsonrpc_error")
        return response

    def stream(self, provider_session_id, prompt):
        try:
            with subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1) as proc:
                initialize_id = f"{provider_session_id}:initialize"
                initialized_id = f"{provider_session_id}:initialized"
                list_id = f"{provider_session_id}:tools-list"
                call_id = f"{provider_session_id}:tools-call"
                initialized = self._request(proc, {"jsonrpc": "2.0", "id": initialize_id, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "butian", "version": "0.1.0"}}})
                if not isinstance(initialized.get("result"), dict):
                    raise RuntimeError("mcp_initialize_missing_result")
                assert proc.stdin is not None
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
                proc.stdin.flush()
                listed = self._request(proc, {"jsonrpc": "2.0", "id": list_id, "method": "tools/list", "params": {}})
                tools = listed.get("result", {}).get("tools", [])
                selected = self._select_tool(tools if isinstance(tools, list) else [])
                if selected is None:
                    yield ProviderEvent(f"{provider_session_id}:unknown", "status", status="unknown", reason="mcp_no_unambiguous_prompt_tool")
                    return
                tool, prompt_field = selected
                called = self._request(proc, {"jsonrpc": "2.0", "id": call_id, "method": "tools/call", "params": {"name": tool["name"], "arguments": {prompt_field: prompt}}})
                result = called.get("result", {})
                content = result.get("content", []) if isinstance(result, dict) else []
                texts = [item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)] if isinstance(content, list) else []
                if result.get("isError") or not texts:
                    yield ProviderEvent(f"{provider_session_id}:unknown", "status", status="unknown", reason="mcp_response_missing_text_or_tool_error")
                    return
                yield ProviderEvent(f"{provider_session_id}:output", "output", "\n".join(texts))
                yield ProviderEvent(f"{provider_session_id}:completed", "status", status="completed")
        except Exception as exc:
            reason = str(exc) if isinstance(exc, RuntimeError) else f"mcp_protocol_error:{type(exc).__name__}"
            yield ProviderEvent(f"{provider_session_id}:unknown", "status", status="unknown", reason=reason)

    def cancel(self, provider_session_id):
        return ProviderResult("unsupported", reason="MCP cancellation capability not declared")
