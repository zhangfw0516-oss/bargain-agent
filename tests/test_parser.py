"""Tests for agent.parse_user_instruction."""

import json
import unittest
from typing import Any

import agent


class FakeChoice:
    def __init__(self, content: str | None, finish_reason: str = "stop"):
        self.message = type("Message", (), {"content": content})()
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, content: str | None, finish_reason: str = "stop"):
        self.choices = [FakeChoice(content, finish_reason)]


class FakeClient:
    def __init__(self, responses: list[Any]):
        self.chat = self
        self.completions = self
        self._responses = responses
        self._calls: list[dict] = []

    def create(self, **kwargs) -> FakeResponse:
        self._calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class TestParseUserInstruction(unittest.TestCase):
    def _ready_task(self, url: str = "https://www.pbtech.co.nz/product/Kindle") -> dict:
        return {
            "schema_version": "1",
            "status": "ready",
            "product_name": "Kindle Paperwhite 16GB",
            "url": url,
            "target_price": "300.00",
            "currency": "NZD",
            "comparison": "lt",
            "frequency_minutes": 60,
            "condition": None,
            "missing_fields": [],
            "clarification_question": None,
        }

    def test_empty_instruction(self):
        with self.assertRaises(agent.ParserError):
            agent.parse_user_instruction("   ", client=FakeClient([]), model="test")

    def test_long_instruction(self):
        with self.assertRaises(agent.ParserError):
            agent.parse_user_instruction("x" * 8001, client=FakeClient([]), model="test")

    def test_ready_task(self):
        payload = self._ready_task()
        client = FakeClient([FakeResponse(json.dumps(payload))])
        result = agent.parse_user_instruction(
            f"Monitor {payload['url']} and notify me when below NZD 300",
            client=client,
            model="test-model",
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["target_price"], "300.00")
        self.assertEqual(client._calls[0]["model"], "test-model")

    def test_needs_clarification(self):
        payload = {
            "schema_version": "1",
            "status": "needs_clarification",
            "product_name": "iPhone",
            "url": None,
            "target_price": "900.00",
            "currency": "NZD",
            "comparison": "lte",
            "frequency_minutes": None,
            "condition": "new",
            "missing_fields": ["url", "frequency_minutes"],
            "clarification_question": "请提供商品链接，并告诉我多久检查一次？",
        }
        client = FakeClient([FakeResponse(json.dumps(payload))])
        result = agent.parse_user_instruction(
            "帮我监控全新 iPhone，不超过 NZD 900",
            client=client,
            model="test",
        )
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIn("url", result["missing_fields"])

    def test_unsupported(self):
        payload = {
            "schema_version": "1",
            "status": "unsupported",
            "product_name": None,
            "url": None,
            "target_price": None,
            "currency": None,
            "comparison": None,
            "frequency_minutes": None,
            "condition": None,
            "missing_fields": [],
            "clarification_question": "目前不支持监控多个商品或跨平台比价。",
        }
        client = FakeClient([FakeResponse(json.dumps(payload))])
        result = agent.parse_user_instruction(
            "帮我找全网最低价的耳机",
            client=client,
            model="test",
        )
        self.assertEqual(result["status"], "unsupported")

    def test_invalid_response_raises(self):
        client = FakeClient([FakeResponse('{"bad": "json"}')])
        with self.assertRaises(agent.ParserError):
            agent.parse_user_instruction(
                "Monitor https://www.pbtech.co.nz/x when below NZD 100",
                client=client,
                model="test",
            )

    def test_incomplete_finish_reason(self):
        client = FakeClient([FakeResponse(json.dumps(self._ready_task()), finish_reason="length")])
        with self.assertRaises(agent.ParserError):
            agent.parse_user_instruction(
                "Monitor https://www.pbtech.co.nz/x when below NZD 100",
                client=client,
                model="test",
            )

    def test_model_exception(self):
        client = FakeClient([RuntimeError("network down")])
        with self.assertRaises(agent.ParserError):
            agent.parse_user_instruction(
                "Monitor https://www.pbtech.co.nz/x when below NZD 100",
                client=client,
                model="test",
            )

    def test_instruction_samples(self):
        samples = [
            {
                "instruction": "Notify me when https://www.pbtech.co.nz/kindle drops below $250 NZD every hour",
                "url": "https://www.pbtech.co.nz/kindle",
                "overrides": {"target_price": "250.00", "comparison": "lt", "frequency_minutes": 60, "currency": "NZD"},
            },
            {
                "instruction": "Alert me if Samsung Galaxy A17 at https://www.pbtech.co.nz/a17 is at most 450 dollars",
                "url": "https://www.pbtech.co.nz/a17",
                "overrides": {
                    "target_price": "450.00",
                    "comparison": "lte",
                    "frequency_minutes": None,
                    "status": "needs_clarification",
                    "missing_fields": ["frequency_minutes"],
                    "clarification_question": "How often should I check?",
                },
            },
            {
                "instruction": "Watch the Creality printer https://www.pbtech.co.nz/k1max for price drops",
                "url": "https://www.pbtech.co.nz/k1max",
                "overrides": {
                    "status": "needs_clarification",
                    "target_price": None,
                    "missing_fields": ["target_price", "frequency_minutes"],
                    "clarification_question": "What is your target price and check frequency?",
                },
            },
        ]
        for sample in samples:
            with self.subTest(instruction=sample["instruction"]):
                base = self._ready_task(url=sample["url"])
                expected = dict(base)
                expected.update(sample["overrides"])
                client = FakeClient([FakeResponse(json.dumps(expected))])
                result = agent.parse_user_instruction(sample["instruction"], client=client, model="test")
                for key, value in sample["overrides"].items():
                    self.assertEqual(result[key], value)

    # R10: model returns a URL that was not present in the user instruction.
    def test_reject_hallucinated_url(self):
        payload = self._ready_task(url="https://www.pbtech.co.nz/hallucinated")
        client = FakeClient([FakeResponse(json.dumps(payload))])
        with self.assertRaises(agent.ParserError):
            agent.parse_user_instruction(
                "Monitor https://www.pbtech.co.nz/real when below NZD 300",
                client=client,
                model="test",
            )

    # R10: model claims ready with a negative/zero price.
    def test_reject_nonpositive_price(self):
        payload = {**self._ready_task(), "target_price": "0.00"}
        client = FakeClient([FakeResponse(json.dumps(payload))])
        with self.assertRaises(agent.ParserError):
            agent.parse_user_instruction(
                "Monitor https://www.pbtech.co.nz/x when below NZD 0",
                client=client,
                model="test",
            )

    # R10: model ignores an explicit frequency requirement.
    def test_reject_missing_frequency(self):
        payload = {**self._ready_task(url="https://www.pbtech.co.nz/x"), "frequency_minutes": None}
        client = FakeClient([FakeResponse(json.dumps(payload))])
        result = agent.parse_user_instruction(
            "Monitor https://www.pbtech.co.nz/x when below NZD 300",
            client=client,
            model="test",
        )
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIn("frequency_minutes", result["missing_fields"])


if __name__ == "__main__":
    unittest.main()
