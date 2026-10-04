import os
import unittest
from unittest.mock import patch

import config
import llm
from tests.fakes import FakeLLM

PROMPT = "summarise_deal.md"


def _positive(data):
    if not isinstance(data.get("n"), int) or data["n"] <= 0:
        raise ValueError("n must be a positive integer")
    return data["n"]


class TestCallJson(unittest.TestCase):
    def test_returns_validated_value(self):
        fake = FakeLLM({PROMPT: {"n": 3}})
        self.assertEqual(llm.call_json(PROMPT, {"x": 1}, validate=_positive, client=fake, model="m"), 3)
        self.assertEqual(fake.calls[0]["model"], "m")
        self.assertIn('"x": 1', fake.calls[0]["user"])

    def test_invalid_output_is_retried_with_feedback(self):
        fake = FakeLLM({PROMPT: [{"n": -1}, {"n": 2}]})
        self.assertEqual(llm.call_json(PROMPT, "hi", validate=_positive, client=fake, model="m"), 2)
        second = fake.calls[1]["messages"]
        self.assertEqual(second[2]["role"], "assistant")
        self.assertIn("rejected", second[3]["content"])

    def test_gives_up_after_retries(self):
        fake = FakeLLM({PROMPT: [{"n": -1}, {"n": -2}]})
        with self.assertRaises(llm.LLMError):
            llm.call_json(PROMPT, "hi", validate=_positive, client=fake, model="m")

    def test_non_json_is_retried(self):
        fake = FakeLLM({PROMPT: ["not json", {"n": 1}]})
        self.assertEqual(llm.call_json(PROMPT, "hi", validate=_positive, client=fake, model="m"), 1)

    def test_truncated_output_raises(self):
        fake = FakeLLM({PROMPT: [{"n": 1}, {"n": 1}]}, finish_reason="length")
        with self.assertRaisesRegex(llm.LLMError, "truncated"):
            llm.call_json(PROMPT, "hi", client=fake, model="m")

    def test_transport_error_is_wrapped_without_detail(self):
        fake = FakeLLM({PROMPT: RuntimeError("secret-token-in-url")})
        with self.assertRaises(llm.LLMError) as ctx:
            llm.call_json(PROMPT, "hi", client=fake, model="m")
        self.assertNotIn("secret", str(ctx.exception))
        self.assertEqual(len(fake.calls), 1)

    def test_rejects_empty_and_oversized_input(self):
        fake = FakeLLM({PROMPT: {"n": 1}})
        with self.assertRaises(llm.LLMError):
            llm.call_json(PROMPT, "   ", client=fake, model="m")
        with self.assertRaises(llm.LLMError):
            llm.call_json(PROMPT, "x" * (llm.MAX_INPUT_CHARS + 1), client=fake, model="m")


class TestAgentSettings(unittest.TestCase):
    ENV = {"LLM_API_KEY": "k", "LLM_API_BASE_URL": "https://a.example", "LLM_MODEL": "base"}

    def test_per_agent_override_falls_back_to_global(self):
        env = {**self.ENV, "LLM_MODEL_SELECT": "strong"}
        with patch.dict(os.environ, env, clear=True), patch.object(config, "_ENV_LOADED", True):
            self.assertEqual(config.llm_settings("select")[2], "strong")
            self.assertEqual(config.llm_settings("rewrite")[2], "base")
            self.assertEqual(config.llm_settings(None), ("k", "https://a.example", "base"))
            self.assertTrue(llm.is_configured("rewrite"))

    def test_missing_settings_are_reported(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(config, "_ENV_LOADED", True):
            self.assertFalse(llm.is_configured())
            with self.assertRaisesRegex(llm.LLMError, "LLM_API_KEY"):
                llm.make_client()


if __name__ == "__main__":
    unittest.main()
