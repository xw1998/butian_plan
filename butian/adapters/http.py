from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Iterable
from ..core.models import ProviderCapabilities, ModelSelection
from .base import ProviderEvent, ProviderResult


class OpenAICompatibleProvider:
    """Small dependency-free adapter for OpenAI-compatible chat endpoints."""
    def __init__(self, base_url: str, *, api_key: str | None = None, provider_id: str = "openai-compatible", model: str = "unknown", context_window: int | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.provider_id = provider_id
        self.default_model = model
        self.context_window = context_window
        # 每个 provider 会话实际使用的模型（create_child_session 传入，stream 真正使用）
        self._session_models: dict[str, str | None] = {}

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(can_create_child_session=True, can_stream_output=False, can_cancel_output=False, context_window=self.context_window)

    def models(self) -> list[str]:
        return [] if self.default_model == "unknown" else [self.default_model]

    def select_model(self, requested_model):
        model = requested_model or (None if self.default_model == "unknown" else self.default_model)
        if model is None:
            return ModelSelection(requested_model, None, "unknown", "provider did not report a model")
        return ModelSelection(requested_model, model, "applied")

    def create_child_session(self, member_id, prompt, model=None):
        effective = model if model else (None if self.default_model == "unknown" else self.default_model)
        session_id = f"http_{uuid.uuid4().hex}"
        self._session_models[session_id] = effective
        return ProviderResult("accepted", provider_event_id=session_id, model=effective)

    def stream(self, provider_session_id: str, prompt: str) -> Iterable[ProviderEvent]:
        request_model = self._session_models.get(provider_session_id)
        if request_model is None:
            request_model = self.default_model if self.default_model != "unknown" else None
        if request_model is None:
            yield ProviderEvent(f"{provider_session_id}:unknown", "status", status="unknown", reason="provider_no_model_selected")
            return
        payload = json.dumps({"model": request_model, "messages": [{"role": "user", "content": prompt}], "stream": False}).encode()
        request = urllib.request.Request(self.base_url + "/chat/completions", data=payload, headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.loads(response.read().decode("utf-8"))
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                yield ProviderEvent(f"{provider_session_id}:unknown", "status", status="unknown", reason="provider_response_missing_choices")
                return
            choice = choices[0]
            message = choice.get("message")
            model = body.get("model") or self.default_model
            if not isinstance(message, dict):
                yield ProviderEvent(f"{provider_session_id}:unknown", "status", status="unknown", reason="provider_response_missing_message")
                return
            tool_calls = message.get("tool_calls")
            if tool_calls is not None:
                if not isinstance(tool_calls, list) or not tool_calls:
                    yield ProviderEvent(f"{provider_session_id}:unknown", "status", status="unknown", reason="provider_tool_call_invalid")
                    return
                for index, tool_call in enumerate(tool_calls):
                    if not isinstance(tool_call, dict):
                        yield ProviderEvent(f"{provider_session_id}:unknown", "status", status="unknown", reason="provider_tool_call_invalid")
                        return
                    call_id = tool_call.get("id")
                    function = tool_call.get("function")
                    if not isinstance(call_id, str) or not isinstance(function, dict) or function.get("type") not in {None, "function"}:
                        yield ProviderEvent(f"{provider_session_id}:unknown", "status", status="unknown", reason="provider_tool_call_invalid")
                        return
                    tool_id = function.get("name")
                    arguments = function.get("arguments")
                    if not isinstance(tool_id, str) or not isinstance(arguments, str):
                        yield ProviderEvent(f"{provider_session_id}:unknown", "status", status="unknown", reason="provider_tool_call_invalid")
                        return
                    try:
                        input_data = json.loads(arguments)
                    except (TypeError, json.JSONDecodeError):
                        yield ProviderEvent(f"{provider_session_id}:unknown", "status", status="unknown", reason="provider_tool_call_invalid_arguments")
                        return
                    yield ProviderEvent(
                        f"{provider_session_id}:tool_call:{index}",
                        "tool_call",
                        model=model,
                        metadata={"call_id": call_id, "tool_id": tool_id, "version": 1, "input": input_data},
                    )
                return
            text = message.get("content")
            if not isinstance(text, str):
                yield ProviderEvent(f"{provider_session_id}:unknown", "status", status="unknown", reason="provider_response_missing_text")
                return
            yield ProviderEvent(f"{provider_session_id}:output", "output", text=text, model=model)
            yield ProviderEvent(f"{provider_session_id}:completed", "status", status="completed", model=model)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            yield ProviderEvent(f"{provider_session_id}:unknown", "status", status="unknown", reason=f"provider_transport_error:{type(exc).__name__}")
        finally:
            self._session_models.pop(provider_session_id, None)

    def cancel(self, provider_session_id):
        return ProviderResult("unsupported", reason="endpoint does not expose cancellation")


class OllamaProvider(OpenAICompatibleProvider):
    def __init__(self, base_url: str = "http://127.0.0.1:11434/v1", *, model: str = "unknown", context_window: int | None = None):
        super().__init__(base_url, provider_id="ollama", model=model, context_window=context_window)
