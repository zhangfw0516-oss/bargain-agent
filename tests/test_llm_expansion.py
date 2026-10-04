"""Offline tests for LLM query expansion and candidate ranking."""

import json
import unittest
from unittest.mock import MagicMock, patch

import agent
import discovery


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


def _candidate(title, url=None, match=discovery.MATCH_SAME):
    return {
        "title": title,
        "url": url or f"https://example.com/{title.lower().replace(' ', '-')}",
        "match": match,
        "reasons": [],
        "retailer": "example",
    }


class TestExpandSearchQueries(unittest.TestCase):
    def test_returns_original_query_when_no_client(self):
        # No API key configured → _make_client raises, caught by expand
        with patch.object(agent, "_make_client", side_effect=Exception("no key")):
            result = agent.expand_search_queries(_request())
        self.assertEqual(result, ["Fitbit Air"])

    def test_returns_original_query_on_empty(self):
        result = agent.expand_search_queries(_request(query="", product_name=""))
        self.assertEqual(result, [])

    def test_deduplicates_and_keeps_original_first(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices[0].finish_reason = "stop"
        mock_response.choices[0].message.content = json.dumps([
            "Fitbit Air",
            "fitbit air black",
            "Google Fitbit Air",
        ])
        mock_client.chat.completions.create.return_value = mock_response
        result = agent.expand_search_queries(
            _request(), client=mock_client, model="test-model"
        )
        self.assertEqual(result[0], "Fitbit Air")
        self.assertIn("fitbit air black", result)
        self.assertIn("Google Fitbit Air", result)
        # No duplicates
        self.assertEqual(len(result), len(set(r.lower() for r in result)))

    def test_caps_at_six_queries(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices[0].finish_reason = "stop"
        mock_response.choices[0].message.content = json.dumps([f"query {i}" for i in range(10)])
        mock_client.chat.completions.create.return_value = mock_response
        result = agent.expand_search_queries(
            _request(), client=mock_client, model="test-model"
        )
        self.assertLessEqual(len(result), 6)


class TestRankCandidates(unittest.TestCase):
    def test_returns_input_when_few_candidates(self):
        candidates = [_candidate("A"), _candidate("B"), _candidate("C")]
        result = agent.rank_candidates(candidates, _request())
        self.assertEqual(result, candidates)

    def test_returns_input_on_failure(self):
        candidates = [_candidate(f"Item {i}") for i in range(5)]
        with patch.object(agent, "_make_client", side_effect=Exception("no key")):
            result = agent.rank_candidates(candidates, _request())
        self.assertEqual(result, candidates)

    def test_reranks_by_llm_score(self):
        candidates = [_candidate(f"Item {i}") for i in range(5)]
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices[0].finish_reason = "stop"
        # Score: item 4 highest, item 0 lowest
        mock_response.choices[0].message.content = json.dumps([1, 2, 3, 4, 10])
        mock_client.chat.completions.create.return_value = mock_response
        result = agent.rank_candidates(
            candidates, _request(), client=mock_client, model="test-model"
        )
        self.assertEqual(result[0]["title"], "Item 4")
        self.assertEqual(result[-1]["title"], "Item 0")
        # All candidates preserved
        self.assertEqual(len(result), len(candidates))


class TestDiscoveryIntegration(unittest.TestCase):
    def test_expand_fn_called_and_multiple_queries_searched(self):
        queries_searched = []

        def web_fn(q):
            queries_searched.append(q)
            return []

        def expand_fn(request):
            return ["Fitbit Air", "fitbit air black"]

        result = discovery.search_and_compare(
            _request(),
            retailers=[],
            web_candidates_fn=web_fn,
            expand_fn=expand_fn,
        )
        self.assertEqual(queries_searched, ["Fitbit Air", "fitbit air black"])
        self.assertEqual(result["counts"]["candidates"], 0)

    def test_rank_fn_called_before_pricing(self):
        rank_called = []

        def rank_fn(candidates, request):
            rank_called.append(len(candidates))
            # Reverse the order
            return list(reversed(candidates))

        def web_fn(q):
            return [
                {"url": "https://example.com/1", "title": "Fitbit Air Black"},
                {"url": "https://example.com/2", "title": "Fitbit Air Obsidian"},
                {"url": "https://example.com/3", "title": "Fitbit Air"},
                {"url": "https://example.com/4", "title": "Fitbit Air Smart Watch"},
            ]

        def web_offer(url):
            return {
                "product_name": "Fitbit Air",
                "price": "100.00",
                "currency": "NZD",
                "in_stock": True,
                "url": url,
            }

        result = discovery.search_and_compare(
            _request(),
            retailers=[],
            web_candidates_fn=web_fn,
            web_offer_fn=web_offer,
            rank_fn=rank_fn,
        )
        self.assertTrue(rank_called)
        self.assertEqual(len(result["offers"]), 4)


if __name__ == "__main__":
    unittest.main()
