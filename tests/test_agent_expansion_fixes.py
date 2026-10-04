"""Offline tests for the LLM expansion/ranking JSON-shape fixes in ``agent.py``.

Both helpers now request ``response_format={"type": "json_object"}`` but still accept
the legacy bare-array shape defensively. Every test injects a fake OpenAI-compatible
client, so there is no network and no real LLM.
"""

import json
import unittest
from unittest.mock import patch

import agent
import config


class FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = type("Message", (), {"content": content})()
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [FakeChoice(content, finish_reason)]


class FakeClient:
    def __init__(self, responses):
        self.chat = self
        self.completions = self
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _request(**overrides):
    base = {
        "query": "Fitbit Air",
        "product_name": "Fitbit Air",
        "colour": "black",
        "capacity": None,
        "budget": "250.00",
        "currency": "NZD",
    }
    base.update(overrides)
    return base


def _candidate(title):
    return {"title": title, "url": f"https://example.com/{title}", "retailer": "example"}


def _client_returning(content, finish_reason="stop"):
    return FakeClient([FakeResponse(content, finish_reason)])


class TestExpandSearchQueries(unittest.TestCase):
    def test_accepts_object_shape(self):
        client = _client_returning(json.dumps({"queries": ["Fitbit Air black", "Google Fitbit Air"]}))
        result = agent.expand_search_queries(_request(), client=client, model="test-model")
        self.assertEqual(result, ["Fitbit Air", "Fitbit Air black", "Google Fitbit Air"])

    def test_accepts_legacy_bare_array(self):
        client = _client_returning(json.dumps(["Fitbit Air black", "Google Fitbit Air"]))
        result = agent.expand_search_queries(_request(), client=client, model="test-model")
        self.assertEqual(result, ["Fitbit Air", "Fitbit Air black", "Google Fitbit Air"])

    def test_dedupes_case_and_whitespace_variants_original_first(self):
        client = _client_returning(json.dumps({
            "queries": ["  fitbit   AIR ", "FITBIT AIR", "Google Fitbit Air"],
        }))
        result = agent.expand_search_queries(_request(), client=client, model="test-model")
        self.assertEqual(result[0], "Fitbit Air")
        self.assertEqual(result, ["Fitbit Air", "Google Fitbit Air"])

    def test_caps_at_six_total(self):
        client = _client_returning(json.dumps({"queries": [f"query {i}" for i in range(10)]}))
        result = agent.expand_search_queries(_request(), client=client, model="test-model")
        self.assertEqual(len(result), 6)
        self.assertEqual(result[0], "Fitbit Air")

    def test_skips_non_string_and_empty_entries(self):
        client = _client_returning(json.dumps({
            "queries": [None, 42, "", "   ", {"q": "x"}, "Google Fitbit Air"],
        }))
        result = agent.expand_search_queries(_request(), client=client, model="test-model")
        self.assertEqual(result, ["Fitbit Air", "Google Fitbit Air"])

    def test_truncates_overlong_entries(self):
        long_query = "a" * 300
        client = _client_returning(json.dumps({"queries": [long_query]}))
        result = agent.expand_search_queries(_request(), client=client, model="test-model")
        self.assertEqual(result[0], "Fitbit Air")
        self.assertEqual(len(result[1]), 200)
        self.assertEqual(result[1], "a" * 200)

    def test_truncates_overlong_original_query(self):
        client = _client_returning(json.dumps({"queries": []}))
        result = agent.expand_search_queries(
            _request(query="b" * 250, product_name="b" * 250),
            client=client,
            model="test-model",
        )
        self.assertEqual(result, ["b" * 200])

    def test_missing_queries_key_falls_back(self):
        client = _client_returning(json.dumps({"items": ["x"]}))
        result = agent.expand_search_queries(_request(), client=client, model="test-model")
        self.assertEqual(result, ["Fitbit Air"])

    def test_object_with_non_list_queries_falls_back(self):
        client = _client_returning(json.dumps({"queries": "not-a-list"}))
        result = agent.expand_search_queries(_request(), client=client, model="test-model")
        self.assertEqual(result, ["Fitbit Air"])

    def test_malformed_json_falls_back(self):
        client = _client_returning("{not valid json")
        result = agent.expand_search_queries(_request(), client=client, model="test-model")
        self.assertEqual(result, ["Fitbit Air"])

    def test_finish_reason_length_falls_back(self):
        client = _client_returning(json.dumps({"queries": ["x"]}), finish_reason="length")
        result = agent.expand_search_queries(_request(), client=client, model="test-model")
        self.assertEqual(result, ["Fitbit Air"])

    def test_empty_content_falls_back(self):
        client = _client_returning("")
        result = agent.expand_search_queries(_request(), client=client, model="test-model")
        self.assertEqual(result, ["Fitbit Air"])

    def test_exception_falls_back(self):
        client = FakeClient([RuntimeError("provider down")])
        result = agent.expand_search_queries(_request(), client=client, model="test-model")
        self.assertEqual(result, ["Fitbit Air"])

    def test_missing_client_falls_back(self):
        with patch.object(agent, "_make_client", side_effect=Exception("no key")):
            result = agent.expand_search_queries(_request())
        self.assertEqual(result, ["Fitbit Air"])

    def test_missing_model_falls_back(self):
        client = _client_returning(json.dumps({"queries": ["x"]}))
        with patch.object(config, "llm_model", return_value=""):
            result = agent.expand_search_queries(_request(), client=client, model=None)
        self.assertEqual(result, ["Fitbit Air"])
        self.assertEqual(client.calls, [])

    def test_uses_json_object_response_format(self):
        client = _client_returning(json.dumps({"queries": ["x"]}))
        agent.expand_search_queries(_request(), client=client, model="test-model")
        self.assertEqual(client.calls[0]["response_format"], {"type": "json_object"})

    def test_blank_request_returns_empty(self):
        result = agent.expand_search_queries(_request(query="", product_name=""))
        self.assertEqual(result, [])


class TestRankCandidates(unittest.TestCase):
    def test_accepts_object_shape_and_reorders(self):
        candidates = [_candidate(f"Item {i}") for i in range(5)]
        client = _client_returning(json.dumps({"scores": [1, 2, 3, 4, 10]}))
        result = agent.rank_candidates(candidates, _request(), client=client, model="test-model")
        self.assertEqual(result[0]["title"], "Item 4")
        self.assertEqual(result[-1]["title"], "Item 0")
        self.assertEqual(len(result), len(candidates))

    def test_accepts_legacy_bare_array(self):
        candidates = [_candidate(f"Item {i}") for i in range(5)]
        client = _client_returning(json.dumps([1, 2, 3, 4, 10]))
        result = agent.rank_candidates(candidates, _request(), client=client, model="test-model")
        self.assertEqual(result[0]["title"], "Item 4")

    def test_wrong_length_returns_input_unchanged(self):
        candidates = [_candidate(f"Item {i}") for i in range(5)]
        client = _client_returning(json.dumps({"scores": [1, 2, 3]}))
        result = agent.rank_candidates(candidates, _request(), client=client, model="test-model")
        self.assertEqual(result, candidates)

    def test_invalid_score_values_return_input_unchanged(self):
        candidates = [_candidate(f"Item {i}") for i in range(5)]
        for bad in ([1, 2, 3, 4, float("nan")], [1, 2, 3, 4, float("inf")],
                    [1, 2, 3, 4, float("-inf")], [1, 2, 3, 4, "5"],
                    [1, 2, 3, 4, None], [1, 2, 3, 4, True]):
            with self.subTest(bad=bad):
                client = _client_returning(json.dumps({"scores": bad}))
                result = agent.rank_candidates(candidates, _request(), client=client, model="test-model")
                self.assertEqual(result, candidates)

    def test_stable_tie_break_preserves_original_order(self):
        candidates = [_candidate(f"Item {i}") for i in range(5)]
        client = _client_returning(json.dumps({"scores": [5, 5, 5, 5, 5]}))
        result = agent.rank_candidates(candidates, _request(), client=client, model="test-model")
        self.assertEqual(
            [c["title"] for c in result],
            [f"Item {i}" for i in range(5)],
        )

    def test_few_candidates_untouched_without_llm_call(self):
        candidates = [_candidate("A"), _candidate("B"), _candidate("C")]
        client = FakeClient([FakeResponse(json.dumps({"scores": [1, 2, 3]}))])
        result = agent.rank_candidates(candidates, _request(), client=client, model="test-model")
        self.assertEqual(result, candidates)
        self.assertEqual(client.calls, [])

    def test_exception_returns_input_unchanged(self):
        candidates = [_candidate(f"Item {i}") for i in range(5)]
        client = FakeClient([RuntimeError("provider down")])
        result = agent.rank_candidates(candidates, _request(), client=client, model="test-model")
        self.assertEqual(result, candidates)

    def test_missing_client_returns_input_unchanged(self):
        candidates = [_candidate(f"Item {i}") for i in range(5)]
        with patch.object(agent, "_make_client", side_effect=Exception("no key")):
            result = agent.rank_candidates(candidates, _request())
        self.assertEqual(result, candidates)

    def test_missing_model_returns_input_unchanged(self):
        candidates = [_candidate(f"Item {i}") for i in range(5)]
        client = FakeClient([FakeResponse(json.dumps({"scores": [1, 2, 3, 4, 5]}))])
        with patch.object(config, "llm_model", return_value=""):
            result = agent.rank_candidates(candidates, _request(), client=client, model=None)
        self.assertEqual(result, candidates)
        self.assertEqual(client.calls, [])

    def test_malformed_json_returns_input_unchanged(self):
        candidates = [_candidate(f"Item {i}") for i in range(5)]
        client = _client_returning("{not valid json")
        result = agent.rank_candidates(candidates, _request(), client=client, model="test-model")
        self.assertEqual(result, candidates)

    def test_finish_reason_length_returns_input_unchanged(self):
        candidates = [_candidate(f"Item {i}") for i in range(5)]
        client = _client_returning(json.dumps({"scores": [1, 2, 3, 4, 5]}), finish_reason="length")
        result = agent.rank_candidates(candidates, _request(), client=client, model="test-model")
        self.assertEqual(result, candidates)

    def test_does_not_mutate_input(self):
        candidates = [_candidate(f"Item {i}") for i in range(5)]
        snapshot = [dict(c) for c in candidates]
        client = _client_returning(json.dumps({"scores": [10, 1, 1, 1, 1]}))
        agent.rank_candidates(candidates, _request(), client=client, model="test-model")
        self.assertEqual(candidates, snapshot)

    def test_uses_json_object_response_format(self):
        candidates = [_candidate(f"Item {i}") for i in range(5)]
        client = _client_returning(json.dumps({"scores": [1, 2, 3, 4, 5]}))
        agent.rank_candidates(candidates, _request(), client=client, model="test-model")
        self.assertEqual(client.calls[0]["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
