from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from butian.adapters.config import build_registry, load_provider_config, load_registry_from_env
from butian.adapters.http import OpenAICompatibleProvider
from butian.adapters.manual import ManualProvider


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ProviderConfigTests(unittest.TestCase):
    def test_build_registry_registers_config_and_manual_default(self):
        config = [
            {"type": "openai-compatible", "provider_id": "deepseek-account", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-test", "model": "deepseek-chat"},
            {"type": "ollama", "model": "qwen2.5:7b"},
        ]
        registry, errors = build_registry(config)
        self.assertEqual(errors, [])
        self.assertEqual(registry.list(), ["deepseek-account", "manual", "ollama"])
        self.assertIsInstance(registry.get("manual"), ManualProvider)

    def test_missing_api_key_env_is_reported_not_silent(self):
        config = [{"type": "openai-compatible", "provider_id": "deepseek-account", "base_url": "https://x/v1", "api_key_env": "BUTIAN_TEST_MISSING_KEY", "model": "m"}]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BUTIAN_TEST_MISSING_KEY", None)
            registry, errors = build_registry(config)
        self.assertEqual(len(errors), 1)
        self.assertIn("api_key_env_not_set", errors[0].reason)
        self.assertEqual(registry.list(), ["manual"])

    def test_unknown_type_reported(self):
        registry, errors = build_registry([{"type": "no-such", "provider_id": "x"}])
        self.assertEqual(len(errors), 1)
        self.assertIn("unknown_provider_type", errors[0].reason)
        self.assertEqual(registry.list(), ["manual"])

    def test_load_provider_config_reads_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "providers.json"
            path.write_text(json.dumps({"providers": [{"type": "manual"}]}), encoding="utf-8")
            self.assertEqual(load_provider_config(path), [{"type": "manual"}])
            self.assertEqual(load_provider_config(Path(tmp) / "absent.json"), [])

    def test_load_registry_from_env_prefers_env_var(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom.json"
            path.write_text(json.dumps({"providers": [{"type": "manual"}]}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"BUTIAN_PROVIDERS_FILE": str(path)}):
                registry, errors, source = load_registry_from_env(Path(tmp))
            self.assertEqual(source, str(path))
            self.assertEqual(registry.list(), ["manual"])
            self.assertEqual(errors, [])

    def test_load_registry_missing_env_file_raises(self):
        with mock.patch.dict(os.environ, {"BUTIAN_PROVIDERS_FILE": "Z:/no/such/file.json"}):
            with self.assertRaises(ValueError):
                load_registry_from_env(Path("."))


class PerSessionModelTests(unittest.TestCase):
    def _run_stream(self, provider, model_requested, prompt="hello"):
        created = provider.create_child_session("m1", prompt, model_requested)
        self.assertEqual(created.status, "accepted")
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(json.dumps({"choices": [{"message": {"content": "ok"}}], "model": captured["body"]["model"]}).encode())

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            events = list(provider.stream(created.provider_event_id, prompt))
        return captured["body"], events

    def test_requested_model_actually_used_in_request(self):
        provider = OpenAICompatibleProvider("https://api.example.com/v1", api_key="k", model="default-model")
        body, events = self._run_stream(provider, "member-chosen-model")
        self.assertEqual(body["model"], "member-chosen-model")
        self.assertEqual(events[-1].status, "completed")
        self.assertEqual(events[-1].model, "member-chosen-model")

    def test_fallback_to_default_model_when_none_requested(self):
        provider = OpenAICompatibleProvider("https://api.example.com/v1", api_key="k", model="default-model")
        body, events = self._run_stream(provider, None)
        self.assertEqual(body["model"], "default-model")
        self.assertEqual(events[-1].status, "completed")

    def test_no_model_reports_unknown(self):
        provider = OpenAICompatibleProvider("https://api.example.com/v1", api_key="k", model="unknown")
        created = provider.create_child_session("m1", "hi", None)
        events = list(provider.stream(created.provider_event_id, "hi"))
        self.assertEqual(events[-1].status, "unknown")
        self.assertEqual(events[-1].reason, "provider_no_model_selected")

    def test_session_model_cleared_after_terminal(self):
        provider = OpenAICompatibleProvider("https://api.example.com/v1", api_key="k", model="m")
        self._run_stream(provider, "m2")
        self.assertEqual(provider._session_models, {})


if __name__ == "__main__":
    unittest.main()
