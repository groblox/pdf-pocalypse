"""Unit tests for ai_client multi-provider & independent eval configuration."""
from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock

from PIL import Image

import ai_client
import haiku


class AIClientConfigTests(unittest.TestCase):
    def setUp(self):
        self._orig_env = os.environ.copy()
        for k in list(os.environ):
            if k.startswith(("A11Y_AI_", "A11Y_EVAL_")) or k in (
                "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "OPENAI_BASE_URL",
            ):
                os.environ.pop(k, None)
        ai_client.configure()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        ai_client.configure()

    def test_default_eval_inherits_primary(self):
        ai_client.configure(primary={"provider": "openai", "model": "gpt-4.1-mini"})
        self.assertFalse(ai_client.eval_is_separate())
        self.assertEqual(ai_client.resolve_provider("primary"), "openai")
        self.assertEqual(ai_client.resolve_provider("eval"), "openai")
        self.assertEqual(ai_client.resolve_model("eval"), "gpt-4.1-mini")

    def test_separate_eval_provider_and_model(self):
        ai_client.configure(
            primary={"provider": "anthropic", "model": "claude-haiku-5-5"},
            eval_cfg={"provider": "gemini", "model": "gemini-2.5-pro"},
        )
        self.assertTrue(ai_client.eval_is_separate())
        self.assertEqual(ai_client.resolve_provider("primary"), "anthropic")
        self.assertEqual(ai_client.resolve_model("primary"), "claude-haiku-5-5")
        self.assertEqual(ai_client.resolve_provider("eval"), "gemini")
        self.assertEqual(ai_client.resolve_model("eval"), "gemini-2.5-pro")
        self.assertNotEqual(ai_client.role_signature("primary"), ai_client.role_signature("eval"))

    def test_precedence_cli_over_env_over_project_over_config(self):
        os.environ["A11Y_EVAL_PROVIDER"] = "gemini"
        os.environ["A11Y_EVAL_MODEL"] = "gemini-2.5-flash"
        ai_client.configure(
            config_yaml={"eval": {"provider": "anthropic", "model": "claude-sonnet-4-6"}},
            project={"eval": {"provider": "ollama", "model": "qwen2.5vl"}},
            eval_cfg={"provider": "openai", "model": "gpt-4.1"},
        )
        self.assertEqual(ai_client.resolve_provider("eval"), "openai")
        self.assertEqual(ai_client.resolve_model("eval"), "gpt-4.1")

        # Without CLI override, env wins over project & config_yaml
        ai_client.configure(
            config_yaml={"eval": {"provider": "anthropic", "model": "claude-sonnet-4-6"}},
            project={"eval": {"provider": "ollama", "model": "qwen2.5vl"}},
        )
        self.assertEqual(ai_client.resolve_provider("eval"), "gemini")
        self.assertEqual(ai_client.resolve_model("eval"), "gemini-2.5-flash")

    def test_invalid_provider_raises(self):
        with self.assertRaises(ValueError):
            ai_client.configure(eval_cfg={"provider": "not-a-real-provider"})

    def test_eval_disabled_with_none(self):
        ai_client.configure(
            primary={"provider": "anthropic", "model": "claude-haiku-5-5"},
            eval_cfg={"provider": "none"},
        )
        self.assertEqual(ai_client.backend("eval"), "none")
        self.assertEqual(ai_client.resolve_model("eval"), "")
        self.assertTrue(ai_client.eval_is_separate())

    def test_haiku_facade_delegates(self):
        ai_client.configure(primary={"provider": "openai", "model": "gpt-4o"})
        self.assertEqual(haiku.backend(), "openai")
        self.assertEqual(haiku.resolve_model("primary"), "gpt-4o")


class AIClientTransportTests(unittest.TestCase):
    def setUp(self):
        self._orig_env = os.environ.copy()
        buf = io.BytesIO()
        Image.new("RGB", (40, 40), (10, 80, 160)).save(buf, "PNG")
        self.sample_png = buf.getvalue()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        ai_client.configure()

    def test_openai_eval_structured_json_call(self):
        os.environ["OPENAI_API_KEY"] = "test-openai-key"
        ai_client.configure(
            primary={"provider": "anthropic", "model": "claude-haiku-5-5"},
            eval_cfg={"provider": "openai", "model": "gpt-4.1"},
        )
        schema = {
            "type": "object",
            "properties": {"nonsense": {"type": "array", "items": {"type": "string"}}},
            "required": ["nonsense"],
            "additionalProperties": False,
        }
        fake_resp = mock.Mock()
        fake_resp.ok = True
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"nonsense": []})}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 12},
        }
        with mock.patch("ai_client.requests.post", return_value=fake_resp) as post:
            out = ai_client.ask_eval(
                "System prompt",
                [ai_client.image_block(self.sample_png), {"type": "text", "text": "Check this"}],
                schema=schema,
                effort="low",
            )
        self.assertEqual(out, {"nonsense": []})
        call_kwargs = post.call_args.kwargs
        self.assertEqual(call_kwargs["json"]["model"], "gpt-4.1")
        self.assertEqual(call_kwargs["json"]["response_format"]["type"], "json_schema")
        user_content = call_kwargs["json"]["messages"][1]["content"]
        self.assertEqual(user_content[0]["type"], "image_url")
        self.assertTrue(user_content[0]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_gemini_eval_strips_additional_properties_and_uses_header(self):
        os.environ["GEMINI_API_KEY"] = "test-gemini-key"
        ai_client.configure(eval_cfg={"provider": "gemini", "model": "gemini-2.5-pro"})
        schema = {
            "type": "object",
            "properties": {"verdict": {"type": "string", "enum": ["pass", "review", "fail"]}},
            "required": ["verdict"],
            "additionalProperties": False,
        }
        fake_resp = mock.Mock()
        fake_resp.ok = True
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": '{"verdict": "pass"}'}]}}],
            "usageMetadata": {"promptTokenCount": 90, "candidatesTokenCount": 8},
        }
        with mock.patch("ai_client.requests.post", return_value=fake_resp) as post:
            out = ai_client.ask_eval(
                "System prompt",
                [ai_client.image_block(self.sample_png), {"type": "text", "text": "Evaluate"}],
                schema=schema,
            )
        self.assertEqual(out, {"verdict": "pass"})
        url = post.call_args.args[0]
        self.assertIn("/models/gemini-2.5-pro:generateContent", url)
        self.assertNotIn("test-gemini-key", url)  # API key stays in header, not URL
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "test-gemini-key")
        gen_cfg = post.call_args.kwargs["json"]["generationConfig"]
        self.assertNotIn("additionalProperties", gen_cfg["responseSchema"])

    def test_ollama_works_without_api_key(self):
        ai_client.configure(eval_cfg={"provider": "ollama", "model": "qwen2.5vl"})
        fake_resp = mock.Mock()
        fake_resp.ok = True
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"choices": [{"message": {"content": "OK"}}]}
        with mock.patch("ai_client.requests.post", return_value=fake_resp) as post:
            out = ai_client.ask_eval("Reply OK", [{"type": "text", "text": "OK?"}])
        self.assertEqual(out, "OK")
        self.assertTrue(post.call_args.args[0].startswith("http://localhost:11434/v1/"))

    def test_fit_alt_trims_cleanly_without_spaces(self):
        ai_client.configure(primary={"provider": "none"})
        long_token = "A" * 200
        trimmed = ai_client.fit_alt(long_token, limit=50)
        self.assertLessEqual(len(trimmed), 51)
        self.assertTrue(trimmed.endswith("…"))


if __name__ == "__main__":
    unittest.main()
