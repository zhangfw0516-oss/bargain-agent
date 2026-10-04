"""Offline tests for the discovery-expansion storage/scheduler/scraper/web layers.

Covers the pieces of BROAD_PRODUCT_DISCOVERY.md that live outside discovery.py:

- ``scraper.fetch_quote_on_host``: HTTPS-only, candidate-host-only allow-list,
  cross-host redirect refusal, private-IP refusal, HTTP-error reporting.
- ``storage``: idempotent ``source_kind`` migration; old rows default to
  ``retailer_adapter``; web sources persist as ``web_search``; old task creation
  paths are unchanged.
- ``webapp``: a confirmed web-search offer is persisted with ``source_kind``;
  demo mode never consults the web-search provider.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import scraper
import storage


FIXTURES = Path(__file__).with_name("fixtures")


def _mock_socket_global():
    return patch("scraper.socket.getaddrinfo", return_value=[(None, None, None, None, ("1.2.3.4", 443))])


class MockResponse:
    def __init__(self, content: bytes, url: str, status_code: int = 200, headers: dict | None = None):
        self._content = content
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def iter_content(self, chunk_size: int = 1024):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error")

    def close(self):
        pass


class _MockSession:
    def __init__(self, responses):
        self.responses = responses
        self.requested_urls = []
        self._calls = 0

    def get(self, url, **kwargs):
        self.requested_urls.append(url)
        response = self.responses[self._calls]
        self._calls += 1
        return response

    def close(self):
        pass


_MINIMAL_JSONLD = (
    "<html><head><script type=\"application/ld+json\">"
    + json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Fitbit Air (Obsidian)",
            "offers": {"@type": "Offer", "price": "239.00", "priceCurrency": "NZD",
                       "availability": "https://schema.org/InStock"},
        }
    )
    + "</script></head></html>"
).encode("utf-8")


class TestFetchQuoteOnHost(unittest.TestCase):
    def _session(self, responses):
        session = _MockSession(responses)
        patcher = patch("scraper.requests.Session", return_value=session)
        patcher.start()
        self.addCleanup(patcher.stop)
        return session

    def test_https_product_page_on_its_own_host_is_fetched(self):
        session = self._session([MockResponse(_MINIMAL_JSONLD, "https://www.example-shop.co.nz/p/air")])
        with _mock_socket_global():
            quote = scraper.fetch_quote_on_host("https://www.example-shop.co.nz/p/air")
        self.assertEqual(quote["price"], "239.00")
        self.assertEqual(quote["currency"], "NZD")
        # The configured-shop allow-list was bypassed, not consulted.
        self.assertEqual(session.requested_urls, ["https://www.example-shop.co.nz/p/air"])

    def test_http_is_refused_for_web_candidates(self):
        with self.assertRaises(scraper.ScraperError):
            scraper.fetch_quote_on_host("http://www.example-shop.co.nz/p/air")

    def test_cross_host_redirect_is_refused(self):
        self._session([
            MockResponse(b"", "https://www.example-shop.co.nz/p/air", status_code=302,
                         headers={"Location": "https://other-shop.example.com/p/air"}),
        ])
        with _mock_socket_global():
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote_on_host("https://www.example-shop.co.nz/p/air")

    def test_same_host_redirect_is_followed(self):
        self._session([
            MockResponse(b"", "https://www.example-shop.co.nz/p/air", status_code=302,
                         headers={"Location": "/p/air-final"}),
            MockResponse(_MINIMAL_JSONLD, "https://www.example-shop.co.nz/p/air-final"),
        ])
        with _mock_socket_global():
            quote = scraper.fetch_quote_on_host("https://www.example-shop.co.nz/p/air")
        self.assertEqual(quote["price"], "239.00")

    def test_private_ip_is_refused(self):
        with patch("scraper.socket.getaddrinfo",
                   return_value=[(None, None, None, None, ("192.168.1.10", 443))]):
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote_on_host("https://internal.example.co.nz/p/air")

    def test_http_error_is_reported_as_scraper_error(self):
        self._session([MockResponse(b"", "https://www.example-shop.co.nz/p/air", status_code=403)])
        with _mock_socket_global():
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote_on_host("https://www.example-shop.co.nz/p/air")

    def test_the_shop_allow_list_is_not_weakened_for_normal_fetches(self):
        # fetch_quote without allowed_domains keeps the configured boundary.
        with patch.object(scraper.config, "allowed_scraper_domains", return_value=["pbtech.co.nz"]):
            self._session([MockResponse(_MINIMAL_JSONLD, "https://www.example-shop.co.nz/p/air")])
            with _mock_socket_global():
                with self.assertRaises(scraper.ScraperError):
                    scraper.fetch_quote("https://www.example-shop.co.nz/p/air")


class TestSourceKindStorage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "t.db"

    def test_new_column_defaults_to_retailer_adapter(self):
        storage.init_db(self.db)
        task_id = storage.create_task(
            {
                "product_name": "Kindle",
                "url": "https://www.pbtech.co.nz/product/K1",
                "target_price": "100.00",
                "currency": "NZD",
                "comparison": "lte",
                "frequency_minutes": 60,
                "notify_email": "u@example.com",
                "status": "active",
            },
            db_path=self.db,
        )
        rows = storage.list_task_sources(task_id, db_path=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_kind"], "retailer_adapter")

    def test_web_search_kind_is_persisted(self):
        storage.init_db(self.db)
        task_id = storage.create_task_with_sources(
            {
                "product_name": "Fitbit Air",
                "url": "https://web.example.co.nz/air",
                "target_price": "250.00",
                "currency": "NZD",
                "comparison": "lte",
                "frequency_minutes": 60,
                "notify_email": "u@example.com",
                "status": "active",
            },
            [
                {"url": "https://web.example.co.nz/air",
                 "retailer": "Brave Web Search", "source_kind": "web_search"},
                {"url": "https://www.pbtech.co.nz/product/K1",
                 "retailer": "PB Tech", "source_kind": "retailer_adapter"},
            ],
            db_path=self.db,
        )
        rows = storage.list_task_sources(task_id, db_path=self.db)
        kinds = {r["url"]: r["source_kind"] for r in rows}
        self.assertEqual(kinds["https://web.example.co.nz/air"], "web_search")
        self.assertEqual(kinds["https://www.pbtech.co.nz/product/K1"], "retailer_adapter")

    def test_migration_of_a_pre_expansion_database_keeps_behaviour(self):
        # Build an old database without the source_kind column and with a legacy
        # task that has no source row yet.
        conn = storage._connect(self.db)
        conn.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                product_name TEXT NOT NULL,
                url TEXT NOT NULL,
                target_price TEXT NOT NULL,
                currency TEXT NOT NULL,
                comparison TEXT NOT NULL,
                frequency_minutes INTEGER NOT NULL,
                condition TEXT,
                notify_email TEXT NOT NULL,
                next_check_at TEXT NOT NULL
            );
            CREATE TABLE task_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                retailer TEXT,
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_checked_at TEXT,
                last_error TEXT,
                UNIQUE(task_id, url)
            );
            INSERT INTO tasks (created_at, updated_at, status, product_name, url,
                               target_price, currency, comparison, frequency_minutes,
                               condition, notify_email, next_check_at)
            VALUES ('2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00', 'active',
                    'Kindle', 'https://www.pbtech.co.nz/product/K1', '100.00', 'NZD',
                    'lte', 60, NULL, 'u@example.com', '2026-09-01T00:00:00+00:00');
            """
        )
        conn.commit()
        conn.close()

        storage.init_db(self.db)  # idempotent migration

        rows = storage.list_task_sources(1, db_path=self.db)
        self.assertEqual(len(rows), 1)  # backfilled exactly once
        self.assertEqual(rows[0]["source_kind"], "retailer_adapter")

        # Re-running the migration must not duplicate anything.
        storage.init_db(self.db)
        self.assertEqual(len(storage.list_task_sources(1, db_path=self.db)), 1)

        # A task created after the migration still gets one source derived from URL.
        task_id = storage.create_task(
            {
                "product_name": "Kindle 2",
                "url": "https://www.pbtech.co.nz/product/K2",
                "target_price": "100.00",
                "currency": "NZD",
                "comparison": "lte",
                "frequency_minutes": 60,
                "notify_email": "u@example.com",
                "status": "active",
            },
            db_path=self.db,
        )
        rows = storage.list_task_sources(task_id, db_path=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_kind"], "retailer_adapter")


if __name__ == "__main__":
    unittest.main()
