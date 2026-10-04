"""Offline live-mode server and fake collaborators for the Stage D live branch.

The live web flow (`webapp --live`) is real parsing, real shops, real web search,
real fetching and real SMTP. None of that can run in an automated acceptance, so
this module substitutes every external collaborator with a deterministic fake —
a controllable fake DeepSeek, two fake shops, a fake Brave web-search provider,
fake detail/check fetchers and an explicit fake sender that records what would
have been sent. The business modules themselves (discovery matching, comparison,
identity gating, scheduling, storage) are NOT faked: the point is to exercise
the real live code paths offline.

Substitution seams (all resolved at call time, so patching works process-wide):

- ``agent.parse_shopping_request``               -> fake_deepseek_parse
- ``discovery.default_enabled_retailers``        -> two fake shops
- ``discovery._default_web_candidates``          -> fake Brave provider
- ``discovery._default_web_offer``               -> fake web detail fetch
- ``scraper.fetch_quote``                        -> fake check fetcher
- ``notifier.notify_deal``                       -> fake sender (records, no SMTP)

The tests in tests/test_webapp.py import these fakes for the offline live-path
regression; this file is also runnable as a server for the live-branch browser
walkthrough (tools_browser_walkthrough_live.js):

    python tools_live_offline_server.py [--port 8907]
        [--sends-file project_progress/browser_evidence_live/sends.jsonl]

Zero network beyond 127.0.0.1, zero LLM calls, zero real shop fetches, zero
SMTP dialling.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from retailers.base import Candidate, Retailer, RetailerError

#: The recipient live mode reports/uses while the fakes are installed. It is a
#: deliberately non-routable example address: nothing here can ever send mail.
LIVE_NOTIFY_EMAIL = "live-offline@example.invalid"

PRODUCT = "AeroBuds Pro"

EN_INSTRUCTION = "find AeroBuds Pro black, budget NZD 200, check every 60 minutes"
ZH_INSTRUCTION = "找 AeroBuds Pro 黑色 预算 200 纽币 每 60 分钟查一次"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------- fake DeepSeek

def fake_deepseek_parse(instruction: str) -> dict[str, Any]:
    """A controllable stand-in for agent.parse_shopping_request (schema v2).

    Returns the fixed "model response" for the walkthrough product. The colour is
    echoed from the user's own words (English or Chinese) so the web layer's
    anti-invention validation passes, exactly like the real parser must.
    """
    import agent

    text = str(instruction or "").strip()
    if not text:
        raise agent.ParserError("Please enter what you are looking for.")
    colour = "黑色" if "黑" in text else "black"
    return {
        "schema_version": "2",
        "status": "ready",
        "product_name": PRODUCT,
        "url": None,
        "budget": "200.00",
        "currency": "NZD",
        "colour": colour,
        "capacity": None,
        "condition": None,
        "comparison": "lte",
        "frequency_minutes": 60,
        "missing_fields": [],
        "clarification_question": None,
    }


# ------------------------------------------------------------------ fake shops

class FakeLiveRetailer(Retailer):
    """A fake shop for the live path: fixed candidates, fixed quotes."""

    def __init__(self, name: str, domain: str,
                 candidates: list[Candidate], quotes: dict[str, dict[str, Any]]):
        self.name = name
        self.domains = (domain,)
        self._candidates = candidates
        self._quotes = quotes

    def search(self, query, *, limit=10):
        return list(self._candidates)[:limit]

    def fetch_offer(self, url: str) -> dict[str, Any]:
        quote = self._quotes.get(url)
        if quote is None:
            raise RetailerError(f"no fake offer configured for {url}")
        out = dict(quote)
        out.setdefault("fetched_at", _ts())
        return out


#: Every URL the fake detail/check fetchers know, with its quote.
LIVE_QUOTES: dict[str, dict[str, Any]] = {
    # Live Tech: confirmed same offer + a wrong-colour rejection case.
    "https://livetech.example/aerobuds-pro-black": {
        "product_name": "AeroBuds Pro Wireless Earbuds - Black",
        "price": "199.00", "currency": "NZD", "in_stock": True,
    },
    "https://livetech.example/aerobuds-pro-lavender": {
        "product_name": "AeroBuds Pro Wireless Earbuds - Lavender",
        "price": "199.00", "currency": "NZD", "in_stock": True,
    },
    # Live Mart: confirmed same offer, an unknown-stock page (colour unstated)
    # and a wrong-generation page.
    "https://livemart.example/aerobuds-pro-black": {
        "product_name": "AeroBuds Pro Wireless Earbuds - Black",
        "price": "189.00", "currency": "NZD", "in_stock": True,
    },
    "https://livemart.example/aerobuds-pro": {
        "product_name": "AeroBuds Pro Wireless Earbuds",
        "price": "169.00", "currency": "NZD", "in_stock": None,
    },
    "https://livemart.example/aerobuds-pro-2": {
        "product_name": "AeroBuds Pro 2 Earbuds - Black",
        "price": "159.00", "currency": "NZD", "in_stock": True,
    },
    "https://livetech.example/aerobuds-charging-cable": {
        "product_name": "AeroBuds Charging Cable",
        "price": "19.00", "currency": "NZD", "in_stock": True,
    },
    # Web-search candidate's own page: the cheapest confirmed offer.
    "https://webshop.example/aerobuds-pro-black": {
        "product_name": "AeroBuds Pro Wireless Earbuds - Black",
        "price": "179.00", "currency": "NZD", "in_stock": True,
    },
}


def _build_live_tech() -> FakeLiveRetailer:
    return FakeLiveRetailer(
        "Live Tech", "livetech.example",
        [
            Candidate(retailer="Live Tech", url="https://livetech.example/aerobuds-pro-black",
                      title="AeroBuds Pro Wireless Earbuds - Black", sku="LT-1"),
            Candidate(retailer="Live Tech", url="https://livetech.example/aerobuds-pro-lavender",
                      title="AeroBuds Pro Wireless Earbuds - Lavender", sku="LT-2"),
            Candidate(retailer="Live Tech", url="https://livetech.example/aerobuds-charging-cable",
                      title="AeroBuds Charging Cable", sku="LT-3"),
        ],
        LIVE_QUOTES,
    )


def _build_live_mart() -> FakeLiveRetailer:
    return FakeLiveRetailer(
        "Live Mart", "livemart.example",
        [
            Candidate(retailer="Live Mart", url="https://livemart.example/aerobuds-pro-black",
                      title="AeroBuds Pro Wireless Earbuds - Black", sku="LM-1"),
            Candidate(retailer="Live Mart", url="https://livemart.example/aerobuds-pro-2",
                      title="AeroBuds Pro 2 Earbuds - Black", sku="LM-2"),
            Candidate(retailer="Live Mart", url="https://livemart.example/aerobuds-pro",
                      title="AeroBuds Pro Wireless Earbuds", sku="LM-3"),
        ],
        LIVE_QUOTES,
    )


def build_live_fake_retailers() -> list[Retailer]:
    return [_build_live_tech(), _build_live_mart()]


# ------------------------------------------------- fake Brave + fake fetchers

def fake_brave_search(query: str) -> list[dict[str, Any]]:
    """Fake web-search provider: one web candidate for the walkthrough product."""
    return [
        {
            "url": "https://webshop.example/aerobuds-pro-black",
            "title": "AeroBuds Pro Wireless Earbuds - Black",
        }
    ]


def fake_web_offer(url: str) -> dict[str, Any]:
    quote = LIVE_QUOTES.get(url)
    if quote is None:
        raise RuntimeError(f"no fake web offer configured for {url}")
    out = dict(quote)
    out.setdefault("url", url)
    out.setdefault("fetched_at", _ts())
    return out


def fake_check_fetcher(url: str) -> dict[str, Any]:
    """The fetcher live-mode checks use while the fakes are installed.

    Every call is recorded so tests and the walkthrough can prove exactly how
    many fetches happened (and that no real fetch can).
    """
    FETCH_CALLS.append(url)
    return fake_web_offer(url)


#: URLs the fake check fetcher has been asked for, in order.
FETCH_CALLS: list[str] = []


# ----------------------------------------------------------------- fake sender

#: Every "would-be" notification recorded by the fake sender.
LIVE_SENDS: list[dict[str, Any]] = []
_SENDS_LOCK = threading.Lock()
_SENDS_FILE: Path | None = None


def configure_sends_file(path: Path | None) -> None:
    global _SENDS_FILE
    _SENDS_FILE = path


def fake_send(email: str, task: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    """Explicit fake sender: records instead of dialling SMTP."""
    record = {
        "to": email,
        "task_id": task.get("id"),
        "quote_retailer": quote.get("retailer"),
        "price": quote.get("price"),
        "at": _ts(),
    }
    with _SENDS_LOCK:
        LIVE_SENDS.append(record)
        if _SENDS_FILE is not None:
            with _SENDS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"success": True, "attempts": 1, "transport": "live-offline-stub"}


def reset_live_sends() -> None:
    with _SENDS_LOCK:
        LIVE_SENDS.clear()


# -------------------------------------------------------------------- patches

def install_live_fakes() -> contextlib.ExitStack:
    """Substitute every external collaborator of the live path, process-wide.

    Returns the ExitStack so the caller can keep the patches active for as long
    as the server/tests need them.
    """
    import agent
    import discovery
    import notifier
    import scraper

    stack = contextlib.ExitStack()
    stack.enter_context(patch.object(agent, "parse_shopping_request", fake_deepseek_parse))
    stack.enter_context(patch.object(discovery, "default_enabled_retailers",
                                     build_live_fake_retailers))
    stack.enter_context(patch.object(discovery, "_default_web_candidates", fake_brave_search))
    stack.enter_context(patch.object(discovery, "_default_web_offer", fake_web_offer))
    stack.enter_context(patch.object(scraper, "fetch_quote", fake_check_fetcher))
    stack.enter_context(patch.object(notifier, "notify_deal", fake_send))
    return stack


# -------------------------------------------------------------------- server

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline live-mode server for the Stage D live-branch walkthrough.")
    default_sends = Path(__file__).with_name("project_progress").joinpath(
        "browser_evidence_live", "sends.jsonl")
    parser.add_argument("--port", type=int, default=8907)
    parser.add_argument("--db-path", default=None,
                        help="Database file. Defaults to an isolated temp file.")
    parser.add_argument("--sends-file", default=str(default_sends),
                        help="JSONL file the fake sender appends to (evidence).")
    args = parser.parse_args()

    # Live mode reads its recipient from config; pin it to the non-routable
    # example address BEFORE .env is loaded so the walkthrough asserts against
    # a known value and the real address never appears.
    os.environ["DEFAULT_NOTIFY_EMAIL"] = LIVE_NOTIFY_EMAIL

    sends_file = Path(args.sends_file)
    sends_file.parent.mkdir(parents=True, exist_ok=True)
    sends_file.write_text("", encoding="utf-8")  # fresh evidence per run
    configure_sends_file(sends_file)

    import webapp

    db_path = Path(args.db_path) if args.db_path else None
    if db_path is None:
        handle = tempfile.NamedTemporaryFile(prefix="webui-live-offline-", suffix=".db",
                                              delete=False)
        handle.close()
        db_path = Path(handle.name)

    with install_live_fakes():
        httpd = webapp.serve(db_path, port=args.port, demo=False)
        print(f"Live-mode OFFLINE server (all externals faked): "
              f"http://127.0.0.1:{args.port}/")
        print(f"Database (temp, isolated): {db_path}")
        print(f"Fake-send evidence: {sends_file}")
        print("Zero LLM / zero real shops / zero Brave / zero SMTP. Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
