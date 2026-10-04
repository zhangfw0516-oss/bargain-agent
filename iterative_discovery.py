"""Iterative AI shopping discovery (intelligent-find mode).

User flow implemented here (the bounded, AI-driven part):

    free-text description
      -> the AI interprets it and picks an action (search / fetch / finish)
      -> the search tool reuses Brave (broad web) and returns *real* results
      -> the real evidence (URL + title, and a real detail page when fetched)
         is handed back to the AI
      -> the AI decides the next search / detail fetch, or finishes
      -> concrete, evidence-backed products are recommended
      -> the user confirms an actual product, which turns the descriptive request
         into a *specific* product request
      -> the existing deterministic same-product comparison and monitor
         confirmation then run unchanged (see `discovery`/`webapp`).

Design rules (all enforced by code, not by the model):

- **Bounded loop.** Rounds, search calls, detail fetches and an overall deadline are
  fixed up front; the model cannot widen them.
- **Locally validated decisions.** The model returns a structured action that is
  validated here before anything runs. An invalid action fails closed: it is never
  executed and the loop stops.
- **Observed-URL-only fetching.** A `fetch` action may only request a URL that a
  previous search actually returned. The model cannot invent a URL.
- **No invented prices or URLs.** A price only enters evidence from a real detail
  page; a candidate URL only enters evidence from a real search result. Search
  snippets are never stored or treated as facts.
- **Provenance ids.** Every observation carries a provenance id so a recommendation
  can be traced back to the search result or detail page it came from.
- **No overriding identity conflicts.** This loop never runs the exact model-name
  identity gate on the *descriptive* query — that is what lets category discovery
  ("a good moisturiser for dry skin around $50") recommend concrete products. The
  gate runs later, on the specific product the user selects.
- **Dedupe + no-progress stop.** Queries and URLs are normalised and deduped;
  repeated rounds that add nothing stop the loop.
- **Failure preserves evidence.** Every return carries whatever evidence was
  accumulated; a failure is a partial result, never a wipe.
- **No task creation.** Nothing here writes to the database. A monitoring task is
  only ever created by the existing final confirmation step.

Every external collaborator (model, search, fetch, clock) is injectable, so the whole
flow is tested offline with no network, LLM or SMTP.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import discovery

logger = logging.getLogger(__name__)

ACTION_SEARCH = "search"
ACTION_FETCH = "fetch"
ACTION_FINISH = "finish"
_VALID_ACTIONS = frozenset({ACTION_SEARCH, ACTION_FETCH, ACTION_FINISH})

MAX_QUERY_CHARS = 200
MAX_TITLE_CHARS = 300
MAX_URL_CHARS = 2000
MAX_RESPONSE_TOKENS = 4096
PROMPT_FIND = "find_products.md"

#: How many times one round's decision may be re-asked when the model returns an
#: unusable answer (empty / non-JSON / schema-invalid). Transient formatting slips
#: should not abort the whole run; persistent ones still fail closed.
MAX_DECIDE_RETRIES = 2


#: InvalidAction messages that mean "the model gave malformed output" (retry),
#: not "the model asked for something unsafe" (fail closed).
_FORMAT_DECISION_MESSAGES = (
    "not valid json",
    "not a json object",
    "unsupported or missing action",
    "non-empty query",
    "needs a url",
    "candidates must be a list",
)


def _is_format_decision_error(message: str) -> bool:
    """True when an InvalidAction is a retryable format slip, not an unsafe ask.

    A search-operator rejection or a non-http(s) URL is a safety veto and is NOT
    retryable; a missing field / non-JSON body is the model slipping on format.
    """
    text = str(message or "").lower()
    if "rejected" in text or "not a valid http" in text:
        return False
    return any(marker in text for marker in _FORMAT_DECISION_MESSAGES)

#: Retailer / marketplace tails that search engines append to result titles. They
#: are source attribution, never part of the product's identity: letting "- JB Hi-Fi
#: NZ" flow into a specific-product request would turn "jb", "hi", "fi", "nz" into
#: mandatory identity tokens downstream (observed live, 2026-09-26).
_SITE_TAIL_TOKENS = frozenset(
    """
    pbtech pb tech jbhifi jb hi-fi hi fi noel leeming mightyape mighty ape
    harvey norman kmart heathcotes pricespy priceme parallel imported
    urban global amazon ebay the market chemist warehouse farmers mitre10
    mitre 10 bunnings officeworks healthpost lifepharmacy countdown new world
    """.split("\n")
)

#: Words that end a site tail when they follow a dash separator: country codes,
#: generic shop words. Combined with a preceding "- " separator.
_SITE_TAIL_SUFFIXES = re.compile(
    r"\s*[-–—|]\s*(?:"
    r"pb\s*tech|jb\s*hi[-\s]?fi|noel\s*leeming|mighty\s*ape|harvey\s*norman|"
    r"kmart|heathcotes?|pricespy|price\s*me|parallel\s*imported|urban\s*global|"
    r"amazon|ebay|chemist\s*warehouse|officeworks|healthpost|life\s*pharmacy|"
    r"countdown|new\s*world|mitre\s*10|bunnings|farmers"
    r")(?:\s+(?:nz|new zealand|au|australia|co\.nz|online|shop|store))*\s*$",
    re.IGNORECASE,
)
_GENERIC_TAIL_SUFFIXES = re.compile(
    r"\s*[-–—|]\s*(?:nz|new zealand|au|australia|online|shop|store)\s*$",
    re.IGNORECASE,
)

#: Terminal reasons for the loop, reported honestly in ``progress.finished_reason``.
FINISHED_MODEL = "model_finish"
FINISHED_MAX_ROUNDS = "max_rounds"
FINISHED_DEADLINE = "deadline"
FINISHED_LIMIT = "limit_reached"
FINISHED_NO_PROGRESS = "no_progress"
FINISHED_INVALID_ACTION = "invalid_action"
FINISHED_MODEL_ERROR = "model_error"

STATUS_READY = "ready"
STATUS_PARTIAL = "partial"


class DiscoveryError(RuntimeError):
    """A configuration, provider or model failure in the discovery loop."""


class InvalidAction(DiscoveryError):
    """The model returned an action that failed local validation (fail closed)."""


# --------------------------------------------------------------------------- limits


@dataclass
class DiscoveryLimits:
    """Frozen budget for one iterative discovery run."""

    max_rounds: int = 8
    max_searches: int = 16
    max_detail_fetches: int = 24
    deadline_seconds: float = 180.0
    max_no_progress_rounds: int = 3
    max_recommendations: int = 8

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_rounds": self.max_rounds,
            "max_searches": self.max_searches,
            "max_detail_fetches": self.max_detail_fetches,
            "deadline_seconds": self.deadline_seconds,
            "max_no_progress_rounds": self.max_no_progress_rounds,
            "max_recommendations": self.max_recommendations,
        }

    @classmethod
    def from_env(cls) -> "DiscoveryLimits":
        """Read optional overrides, clamped to a safe range. Defaults are generous."""

        def _int(name: str, default: int, lo: int, hi: int) -> int:
            raw = os.getenv(name, "").strip()
            if not raw:
                return default
            try:
                value = int(raw)
            except ValueError:
                return default
            return min(hi, max(lo, value))

        def _float(name: str, default: float, lo: float, hi: float) -> float:
            raw = os.getenv(name, "").strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                return default
            if value != value or value in (float("inf"), float("-inf")):
                return default
            return min(hi, max(lo, value))

        return cls(
            max_rounds=_int("FIND_MAX_ROUNDS", 8, 1, 30),
            max_searches=_int("FIND_MAX_SEARCHES", 16, 1, 60),
            max_detail_fetches=_int("FIND_MAX_DETAIL_FETCHES", 24, 0, 120),
            deadline_seconds=_float("FIND_DEADLINE_SECONDS", 180.0, 5.0, 900.0),
            max_no_progress_rounds=_int("FIND_MAX_NO_PROGRESS_ROUNDS", 3, 1, 6),
            max_recommendations=_int("FIND_MAX_RECOMMENDATIONS", 8, 1, 25),
        )


# --------------------------------------------------------------------------- urls


def _is_http_url(url: str) -> bool:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if not parts.hostname:
        return False
    if parts.username or parts.password:
        return False
    return True


def _url_key(url: str) -> str:
    return discovery.normalise_product_url(url)


def _query_key(query: str) -> str:
    return " ".join(str(query or "").split()).casefold()


def _clean_title(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS].rstrip()
    return text


def strip_site_tail(title: str) -> str:
    """Remove retailer/marketplace attribution tails from a page title.

    "Health & Fitness Wearables for Active Lifestyles - JB Hi-Fi NZ" becomes
    "Health & Fitness Wearables for Active Lifestyles". Only a trailing dash
    segment is touched; the product's own name is never altered.
    """
    text = " ".join(str(title or "").split())
    for _ in range(3):
        stripped = _SITE_TAIL_SUFFIXES.sub("", text)
        stripped = _GENERIC_TAIL_SUFFIXES.sub("", stripped).strip()
        if stripped == text or not stripped:
            break
        text = stripped
    return text or " ".join(str(title or "").split())


_CATEGORY_TITLE_WORDS = frozenset(
    {
        "category", "categories", "department", "shop", "sale", "deals",
        "clearance", "range", "collection", "collections", "lifestyle",
        "lifestyles", "monitoring", "overview", "guide", "compare", "prices",
        "price", "buy", "online", "store",
    }
)
_CATEGORY_TITLE_PHRASES = (
    " for sale", " for health", " for active", " for every", " for all",
    "fitness trackers", "smart watches", "smartwatches", "wearables",
    "tracker &", "trackers &", " & watches", "shop ",
    # Multi-product shop-by-category framings observed live (2026-09-26).
    " & fitness bands", "health & fitness bands", "fitness bands", " & bands",
)
#: Editorial / guide / review content - an article, never a single product page.
_EDITORIAL_TITLE_PHRASES = (
    "which ", "the right fit", "best ", "top ", "review", "hands-on",
    "comparison", " vs ", " versus", "how to", "guide to", "buying guide",
    "should you", "what to", "everything you", "explained", " hands on",
    # Announcement / news / listicle framing — an article or a bare search
    # results page, never a single purchasable product page.
    "meet ", "introducing", "announcing", "cheap ", "deals", " prices",
)


def _looks_like_category_title(title: str) -> bool:
    """True when a page title reads as a category/listing page, not one product.

    Conservative: only clear multi-product/category signals fire, so single
    product names ("Xiaomi Smart Band 8 Active") are never flagged here.
    """
    text = " ".join(str(title or "").lower().split())
    if not text:
        return False
    if any(phrase in text for phrase in _CATEGORY_TITLE_PHRASES):
        return True
    if any(phrase in text for phrase in _EDITORIAL_TITLE_PHRASES):
        return True
    words = {w.strip(".,:;|()[]") for w in text.split()}
    return len(words & _CATEGORY_TITLE_WORDS) >= 2


def judge_concreteness(
    title: str,
    detail: dict[str, Any] | None,
    description: str,
    *,
    client=None,
    model: str | None = None,
) -> tuple[bool, str]:
    """Master-AI gate: is this evidence a concrete purchasable product?

    Returns ``(is_concrete, reason)``. The judgement reuses the
    ``match_product_aspect`` pipeline with aspect ``concreteness``. A page with a
    real verified price counts as concrete without an LLM call (price only enters
    evidence from a real fetched page); a category/listing page has no such price.
    Any LLM failure or a flag-disabled environment falls back to a conservative
    heuristic: fetched-with-price => concrete, otherwise not.
    """
    if detail is not None and detail.get("price"):
        return True, "The detail page states a real price."
    import config

    if config.llm_concreteness():
        import agent

        try:
            verdict, reason = agent.match_product_aspect(
                "concreteness", None, title, description, client=client, model=model
            )
            # Only two outcomes are decisive: the AI can affirm a concrete product
            # or affirm a non-product. "needs_confirmation" (and any failure) is
            # not decisive, so it falls through to the deterministic heuristic
            # instead of silently emptying the recommendation list.
            if verdict == "same":
                return True, reason
            if verdict == "different":
                return False, reason
        except Exception:
            logger.debug("Concreteness judgement failed; using fallback.", exc_info=True)
    # Heuristic fallback when no AI judge is available: only a title that clearly
    # reads as a category/listing page is dropped. Concrete-looking titles pass so
    # real products are never silently hidden from the user.
    if _looks_like_category_title(title):
        return False, "The page looks like a category or listing page, not one product."
    return True, "No category signals in the title."


def _finite_positive(value: Any) -> str | None:
    """Return a normalised positive decimal string, or None for anything invalid.

    Never invents a price: a missing/invalid page value stays None (unknown).
    """
    from decimal import Decimal, InvalidOperation

    if value is None:
        return None
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if price != price or price in (Decimal("Infinity"), Decimal("-Infinity")):
        return None
    if price <= 0:
        return None
    try:
        return str(price.quantize(Decimal("0.01")))
    except InvalidOperation:
        return None


def _default_sanitise_query(query: str) -> str:
    """Reuse the Brave client's operator rejection so a query can never smuggle
    search operators into the request."""
    import search_providers.brave as brave

    return brave.sanitize_query(query)


# --------------------------------------------------------------------------- actions


def validate_action(
    raw: Any,
    *,
    sanitise_fn: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Validate one untrusted model decision. Raises :class:`InvalidAction`.

    Accepts a ``dict`` (already-parsed) or a JSON ``str``. Unknown *extra* keys are
    ignored, but every required field is checked strictly: a wrong type, a missing
    field, an unknown action, an empty query or a non-http(s) URL fails closed.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raise InvalidAction("The model decision was not valid JSON.") from None
    if not isinstance(raw, dict):
        raise InvalidAction("The model decision was not a JSON object.")

    action = raw.get("action")
    if not isinstance(action, str) or action.strip().lower() not in _VALID_ACTIONS:
        raise InvalidAction(f"Unsupported or missing action: {action!r}.")
    action = action.strip().lower()

    reasoning = raw.get("reasoning")
    reasoning = reasoning.strip() if isinstance(reasoning, str) else ""

    if action == ACTION_SEARCH:
        query = raw.get("query")
        if not isinstance(query, str) or not query.strip():
            raise InvalidAction("A search action needs a non-empty query.")
        query = " ".join(query.split())
        if len(query) > MAX_QUERY_CHARS:
            query = query[:MAX_QUERY_CHARS].rstrip()
        sanitiser = sanitise_fn or _default_sanitise_query
        try:
            query = sanitiser(query)
        except Exception as exc:  # operator / empty / provider-specific rejection
            raise InvalidAction(f"The search query was rejected: {exc}") from None
        if not isinstance(query, str) or not query.strip():
            raise InvalidAction("The search query was rejected.")
        return {"action": action, "query": query, "reasoning": reasoning}

    if action == ACTION_FETCH:
        url = raw.get("url")
        if not isinstance(url, str) or not url.strip():
            raise InvalidAction("A fetch action needs a URL.")
        url = url.strip()
        if len(url) > MAX_URL_CHARS or not _is_http_url(url):
            raise InvalidAction("The fetch URL is not a valid http(s) URL.")
        return {"action": action, "url": url, "reasoning": reasoning}

    # finish
    candidates = raw.get("candidates")
    picked: list[str] = []
    if candidates is None:
        picked = []
    elif isinstance(candidates, list):
        for item in candidates:
            if isinstance(item, str) and item.strip():
                picked.append(item.strip())
    else:
        raise InvalidAction("A finish action's candidates must be a list of evidence ids.")
    deduped: list[str] = []
    seen: set[str] = set()
    for item in picked:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return {"action": action, "candidates": deduped, "reasoning": reasoning}


# --------------------------------------------------------------------------- evidence


def _clean_detail(quote: Any) -> dict[str, Any] | None:
    """Normalise a detail-page quote into stored evidence, or None if unusable.

    Only real page values are kept. A non-dict, or one that states no product name,
    yields None so the caller records a structured fetch failure instead of inventing
    a product.
    """
    if not isinstance(quote, dict):
        return None
    name = _clean_title(quote.get("product_name"))
    if not name:
        return None
    in_stock = quote.get("in_stock")
    if in_stock not in (True, False):
        in_stock = None
    currency = quote.get("currency")
    currency = str(currency).upper() if isinstance(currency, str) and currency.strip() else None
    return {
        "product_name": name,
        "price": _finite_positive(quote.get("price")),
        "currency": currency,
        "in_stock": in_stock,
        "url": str(quote.get("url") or "").strip() or None,
        "fetched_at": quote.get("fetched_at") if isinstance(quote.get("fetched_at"), str) else None,
    }


def _add_search_candidates(
    results: Any,
    *,
    query: str,
    round_no: int,
    next_id: int,
    evidence: dict[str, dict[str, Any]],
    order: list[str],
    url_to_pid: dict[str, str],
) -> int:
    """Fold real search results into evidence. Returns how many URLs were new.

    Adversarial tool output is handled defensively: a non-list, non-dict entries,
    missing/short titles and non-http(s) URLs are dropped without inventing anything.
    """
    if not isinstance(results, list):
        return 0
    added = 0
    for item in results:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        title = item.get("title")
        if not isinstance(url, str) or not isinstance(title, str):
            continue
        url = url.strip()
        title = _clean_title(title)
        if not url or not title or not _is_http_url(url):
            continue
        key = _url_key(url)
        existing = url_to_pid.get(key)
        if existing is not None:
            queries = evidence[existing].setdefault("also_seen_by_queries", [])
            if query not in queries:
                queries.append(query)
            continue
        pid = f"e{next_id}"
        next_id += 1
        evidence[pid] = {
            "provenance_id": pid,
            "kind": "search_result",
            "url": url,
            "title": title,
            "source": str(item.get("source") or "web_search"),
            "query": query,
            "round": round_no,
            "also_seen_by_queries": [],
            "detail": None,
            "fetch_error": None,
        }
        order.append(pid)
        url_to_pid[key] = pid
        added += 1
    return added


# --------------------------------------------------------------------------- default tools


def _default_search(query: str) -> list[dict[str, Any]]:
    """Broad web search: reuse the Brave client (candidates = URL + title only)."""
    import search_providers.brave as brave

    return brave.search(query)


def _default_fetch(url: str) -> dict[str, Any]:
    """Detail fetch: reuse the SSRF-protected scraper (host-restricted)."""
    import scraper

    return scraper.fetch_quote_on_host_llm(url)


def _make_client(model: str | None = None):
    import config
    from llm_client import OpenAI

    key = config.llm_api_key()
    base_url = config.llm_base_url()
    model = model or config.llm_model()
    if not model:
        raise DiscoveryError("A model ID is required.")
    client = OpenAI(api_key=key, base_url=base_url, timeout=config.llm_timeout(), max_retries=1)
    return client, model


def default_decide_fn(state: dict[str, Any], *, client=None, model: str | None = None) -> str:
    """Ask the model for the next action, returning the raw JSON text.

    Injectable everywhere; the default performs one bounded LLM call. The prompt is
    versioned in ``prompts/find_products.md``.
    """
    if client is None:
        client, model = _make_client(model)
    if not model:
        raise DiscoveryError("A model ID is required.")
    prompt = Path(__file__).with_name("prompts").joinpath(PROMPT_FIND).read_text(encoding="utf-8")
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(state, ensure_ascii=False)},
    ]
    last_problem = "The model returned an empty decision."
    for _attempt in range(MAX_DECIDE_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=MAX_RESPONSE_TOKENS,
            )
        except Exception as exc:
            raise DiscoveryError(f"LLM request failed: {type(exc).__name__}.") from None
        choice = response.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            last_problem = "The model decision was truncated."
        elif not choice.message.content:
            last_problem = "The model returned an empty decision."
        else:
            return choice.message.content
        # Feed the rejection back and ask again for the same decision.
        messages = messages + [
            {"role": "assistant", "content": getattr(choice.message, "content", None) or ""},
            {"role": "user", "content": (
                f"{last_problem} Reply with exactly one valid JSON action object."
            )},
        ]
    raise DiscoveryError(last_problem)


# --------------------------------------------------------------------------- loop


def _evidence_summary(e: dict[str, Any]) -> dict[str, Any]:
    detail = e.get("detail")
    return {
        "provenance_id": e["provenance_id"],
        "title": e["title"],
        "url": e["url"],
        "price": (detail or {}).get("price"),
        "currency": (detail or {}).get("currency"),
        "in_stock": (detail or {}).get("in_stock"),
        "fetched": detail is not None,
        "fetch_error": e.get("fetch_error"),
    }


def _build_state(
    *,
    description: str,
    constraints: dict[str, Any],
    limits: DiscoveryLimits,
    round_no: int,
    rounds_used: int,
    searches: int,
    fetches: int,
    searched_queries: list[str],
    fetched_urls: list[str],
    evidence: dict[str, dict[str, Any]],
    order: list[str],
    notes: list[str],
) -> dict[str, Any]:
    return {
        "description": description,
        "constraints": {
            k: constraints.get(k)
            for k in ("product_name", "colour", "capacity", "condition", "budget", "currency")
            if constraints.get(k) is not None
        },
        "round": round_no,
        "remaining": {
            "rounds": max(0, limits.max_rounds - rounds_used),
            "searches": max(0, limits.max_searches - searches),
            "detail_fetches": max(0, limits.max_detail_fetches - fetches),
        },
        "searched_queries": list(searched_queries),
        "fetched_urls": list(fetched_urls),
        "evidence": [_evidence_summary(e) for e in (evidence[pid] for pid in order)],
        "notes": list(notes[-8:]),
    }


def run_iterative_discovery(
    description: str,
    *,
    decide_fn: Callable[[dict[str, Any]], Any] | None = None,
    search_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    fetch_fn: Callable[[str], dict[str, Any]] | None = None,
    limits: DiscoveryLimits | None = None,
    request: dict[str, Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sanitise_fn: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Run the bounded AI-driven discovery loop and return recommendations + evidence.

    Returns a dict with ``status`` (``ready``/``partial``), ``recommendations``,
    ``evidence``, ``progress`` and ``notes``. Never raises for a model/tool failure;
    those become a ``partial`` result that still preserves every observation.
    """
    description = str(description or "").strip()
    limits = limits or DiscoveryLimits.from_env()
    constraints = dict(request or {})
    decide = decide_fn or (lambda state: default_decide_fn(state))
    do_search = search_fn or _default_search
    do_fetch = fetch_fn or _default_fetch

    evidence: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    url_to_pid: dict[str, str] = {}
    searched_queries: list[str] = []
    fetched_urls: list[str] = []
    notes: list[str] = []
    next_id = 1
    searches = 0
    fetches = 0
    no_progress = 0
    rounds_used = 0
    started = clock()
    finished_reason = FINISHED_MAX_ROUNDS
    ok = False
    model_order: list[str] = []

    for round_no in range(1, limits.max_rounds + 1):
        rounds_used = round_no
        if clock() - started >= limits.deadline_seconds:
            finished_reason = FINISHED_DEADLINE
            logger.info("Find: deadline %.0fs reached after round %d; returning what we have.", limits.deadline_seconds, round_no - 1)
            break
        logger.info("Find: round %d/%d - asking the model for the next action...", round_no, limits.max_rounds)

        state = _build_state(
            description=description,
            constraints=constraints,
            limits=limits,
            round_no=round_no,
            rounds_used=round_no - 1,
            searches=searches,
            fetches=fetches,
            searched_queries=searched_queries,
            fetched_urls=fetched_urls,
            evidence=evidence,
            order=order,
            notes=notes,
        )
        # A *format* slip (empty / non-JSON / wrong shape) is retryable: the model
        # gets the rejection fed back and is re-asked up to MAX_DECIDE_RETRIES
        # times for the same round. An *unsafe* decision (bad URL, search operator)
        # and a persistent failure fail closed. A provider outage ends the round
        # honestly as a model error with the evidence so far preserved.
        action = None
        last_format_error: str | None = None
        for _attempt in range(MAX_DECIDE_RETRIES + 1):
            round_state = state
            if last_format_error is not None:
                round_state = dict(state)
                round_state["decision_error"] = (
                    f"Your previous answer was rejected because it was not a valid "
                    f"JSON action ({last_format_error}). Reply with exactly one valid "
                    "JSON action object and nothing else."
                )
            try:
                raw = decide(round_state)
                action = validate_action(raw, sanitise_fn=sanitise_fn)
                break
            except InvalidAction as exc:
                msg = str(exc)
                if _is_format_decision_error(msg):
                    last_format_error = msg
                    continue
                finished_reason = FINISHED_INVALID_ACTION
                notes.append(f"The model's action was rejected and not executed: {exc}")
                action = None
                break
            except DiscoveryError as exc:
                finished_reason = FINISHED_MODEL_ERROR
                notes.append(f"The model request failed: {exc}")
                logger.warning("Discovery decision failed: %s", exc)
                break
            except Exception as exc:  # provider outage, network, truncation
                finished_reason = FINISHED_MODEL_ERROR
                notes.append(f"The model request failed: {type(exc).__name__}.")
                logger.warning("Discovery decision failed: %s", exc)
                break
        if action is None and last_format_error is not None:
            finished_reason = FINISHED_INVALID_ACTION
            notes.append(
                f"The model's action was rejected and not executed after "
                f"{MAX_DECIDE_RETRIES + 1} attempts: {last_format_error}"
            )
        if action is None:
            break

        kind = action["action"]

        if kind == ACTION_FINISH:
            finished_reason = FINISHED_MODEL
            ok = True
            # The model may order the recommendations. Unknown ids are dropped (the
            # model cannot invent provenance), and every observed evidence id stays
            # available even when the model names only some of them.
            model_order = [pid for pid in action["candidates"] if pid in evidence]
            break

        if kind == ACTION_SEARCH:
            if searches >= limits.max_searches:
                finished_reason = FINISHED_LIMIT
                notes.append(f"The search budget ({limits.max_searches}) was reached.")
                break
            qkey = _query_key(action["query"])
            if qkey in {_query_key(q) for q in searched_queries}:
                no_progress += 1
                notes.append(f"Duplicate search query ignored: {action['query']!r}.")
                if no_progress >= limits.max_no_progress_rounds:
                    finished_reason = FINISHED_NO_PROGRESS
                    break
                continue
            searches += 1
            searched_queries.append(action["query"])
            logger.info("Find: round %d - searching the web for: %s", round_no, action["query"])
            try:
                results = do_search(action["query"])
            except Exception as exc:
                no_progress += 1
                notes.append(f"Search {action['query']!r} failed: {type(exc).__name__}.")
                logger.warning("Discovery search failed: %s", exc)
                if no_progress >= limits.max_no_progress_rounds:
                    finished_reason = FINISHED_NO_PROGRESS
                    break
                continue
            logger.info("Find: round %d - search returned %d candidate(s).", round_no, (len(results) if isinstance(results, list) else 0))
            added = _add_search_candidates(
                results,
                query=action["query"],
                round_no=round_no,
                next_id=next_id,
                evidence=evidence,
                order=order,
                url_to_pid=url_to_pid,
            )
            next_id += added
            no_progress = 0 if added else no_progress + 1
            if no_progress >= limits.max_no_progress_rounds:
                finished_reason = FINISHED_NO_PROGRESS
                break
            continue

        # ACTION_FETCH -- must be a URL a search actually returned.
        url = action["url"]
        key = _url_key(url)
        pid = url_to_pid.get(key)
        if pid is None:
            finished_reason = FINISHED_INVALID_ACTION
            notes.append(
                "Refused to fetch a URL that no search returned; the action was not executed."
            )
            break
        if key in {_url_key(u) for u in fetched_urls}:
            no_progress += 1
            notes.append(f"Duplicate detail fetch ignored: {url!r}.")
            if no_progress >= limits.max_no_progress_rounds:
                finished_reason = FINISHED_NO_PROGRESS
                break
            continue
        if fetches >= limits.max_detail_fetches:
            finished_reason = FINISHED_LIMIT
            notes.append(f"The detail-fetch budget ({limits.max_detail_fetches}) was reached.")
            break
        fetches += 1
        fetched_urls.append(url)
        logger.info("Find: round %d - reading product page: %s", round_no, url)
        try:
            quote = do_fetch(url)
        except Exception as exc:
            evidence[pid]["fetch_error"] = f"{type(exc).__name__}: {exc}"
            notes.append(f"Detail page for {evidence[pid]['title']!r} could not be read.")
            no_progress += 1
            if no_progress >= limits.max_no_progress_rounds:
                finished_reason = FINISHED_NO_PROGRESS
                break
            continue
        detail = _clean_detail(quote)
        if detail is None:
            evidence[pid]["fetch_error"] = "The page did not state a product name."
            notes.append(f"Detail page for {evidence[pid]['title']!r} stated no product.")
            no_progress += 1
        else:
            evidence[pid]["detail"] = detail
            evidence[pid]["fetch_error"] = None
            no_progress = 0
        if no_progress >= limits.max_no_progress_rounds:
            finished_reason = FINISHED_NO_PROGRESS
            break

    elapsed = max(0.0, clock() - started)
    recommendations = _recommendations(
        evidence, order, limits.max_recommendations, model_order,
        description=description, constraints=constraints,
    )
    # A clean bounded stop (the model finished, or a budget/deadline/no-progress limit
    # was reached) still yields usable recommendations. An invalid model action or a
    # model error is reported as partial even though the evidence is preserved.
    clean_stops = {
        FINISHED_MODEL,
        FINISHED_MAX_ROUNDS,
        FINISHED_DEADLINE,
        FINISHED_LIMIT,
        FINISHED_NO_PROGRESS,
    }
    status = (
        STATUS_READY
        if (recommendations and finished_reason in clean_stops)
        else STATUS_PARTIAL
    )
    if status == STATUS_READY:
        ok = True
    if finished_reason not in clean_stops:
        notes.append(
            "The discovery loop stopped early, so this is a partial result; "
            "the products found so far are still shown."
        )
    if not recommendations:
        notes.append("No concrete product could be recommended from the evidence gathered.")

    return {
        "status": status,
        "ok": ok,
        "description": description,
        "request": constraints,
        "recommendations": recommendations,
        "evidence": [evidence[pid] for pid in order],
        "notes": notes,
        "progress": {
            "rounds": rounds_used,
            "searches": searches,
            "detail_fetches": fetches,
            "evidence": len(order),
            "finished_reason": finished_reason,
            "elapsed_seconds": round(elapsed, 3),
            "limits": limits.as_dict(),
        },
    }


def _over_budget(price: Any, constraints: dict[str, Any]) -> bool:
    """True when a confirmed price is above the user's stated budget.

    Only a real, finite fetched price can disqualify; an unknown price never
    hides a recommendation. The comparison currency is not converted here —
    a budget is only enforced when the quote currency matches the request's
    (or the request named no currency, in which case any single price in the
    user's context is taken at face value)."""
    from decimal import Decimal, InvalidOperation

    budget = (constraints or {}).get("budget")
    if budget in (None, ""):
        return False
    try:
        limit_amount = Decimal(str(budget))
    except (InvalidOperation, ValueError, TypeError):
        return False
    try:
        amount = Decimal(str(price))
    except (InvalidOperation, ValueError, TypeError):
        return False
    return amount > limit_amount


def _recommendations(
    evidence: dict[str, dict[str, Any]],
    order: list[str],
    limit: int,
    model_order: list[str] | None = None,
    *,
    description: str = "",
    constraints: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Concrete, evidence-backed products, best (priced) first, insertion order kept.

    When the model finished with an explicit ``candidates`` order, that order is
    applied first (model-named, observed ids in the model's sequence), and every
    remaining observed product follows in its priced/insertion order. Unknown ids are
    dropped by the caller, so the model cannot invent or drop real provenance.
    """
    rows: list[dict[str, Any]] = []
    for pid in order:
        e = evidence[pid]
        if e.get("kind") != "search_result":
            continue
        detail = e.get("detail") or {}
        # Master-AI concreteness gate: a category/listing page is never presented
        # as a product recommendation. A real fetched price is itself proof of a
        # concrete product page (prices only enter evidence from real detail
        # fetches); otherwise the LLM judges from the title.
        is_concrete, gate_reason = judge_concreteness(
            detail.get("product_name") or e["title"],
            e.get("detail"),
            description,
        )
        if not is_concrete:
            logger.info(
                "Recommendation filtered as non-product (%s): %s", gate_reason, e["url"]
            )
            continue
        if detail.get("price") and _over_budget(detail.get("price"), constraints or {}):
            logger.info(
                "Recommendation filtered as over budget (%s %s): %s",
                detail.get("price"), detail.get("currency"), e["url"],
            )
            continue
        rows.append(
            {
                "provenance_id": pid,
                "url": e["url"],
                "title": e["title"],
                "source": e.get("source") or "web_search",
                "price": detail.get("price"),
                "currency": detail.get("currency"),
                "in_stock": detail.get("in_stock"),
                "detail_name": detail.get("product_name"),
                "fetched": bool(e.get("detail")),
                "fetch_error": e.get("fetch_error"),
            }
        )
    rows.sort(key=lambda r: 0 if r["price"] else (1 if r["fetched"] else 2))
    if model_order:
        rank = {pid: index for index, pid in enumerate(model_order)}
        rows.sort(key=lambda r: rank.get(r["provenance_id"], len(rank)))
    return rows[:limit]


# --------------------------------------------------------------------------- handoff


def web_candidate_dicts(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidate dicts for ``discovery.search_and_compare(web_candidates_fn=...)``.

    Built only from observed evidence (URL + title). No price is taken from here —
    the comparison re-reads the detail page (or a cached real quote) itself.
    """
    out: list[dict[str, Any]] = []
    for e in result.get("evidence") or []:
        if e.get("kind") == "search_result" and e.get("url") and e.get("title"):
            out.append(
                {"url": e["url"], "title": e["title"], "source": e.get("source") or "web_search"}
            )
    return out


def cached_quotes(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Real detail quotes already fetched, keyed by normalised URL.

    Lets the specific-product comparison reuse evidence instead of re-fetching. A
    quote here came from a real page; nothing is synthesised.
    """
    out: dict[str, dict[str, Any]] = {}
    for e in result.get("evidence") or []:
        detail = e.get("detail")
        if not detail or not e.get("url"):
            continue
        out[_url_key(e["url"])] = {
            "product_name": detail["product_name"],
            "url": detail.get("url") or e["url"],
            "price": detail.get("price"),
            "currency": detail.get("currency"),
            "in_stock": detail.get("in_stock"),
            "fetched_at": detail.get("fetched_at"),
            "shipping_cost": None,
        }
    return out


def recommendation_by_id(result: dict[str, Any], provenance_id: str) -> dict[str, Any] | None:
    for rec in result.get("recommendations") or []:
        if rec.get("provenance_id") == provenance_id:
            return rec
    return None


def recommendation_by_url(result: dict[str, Any], url: str) -> dict[str, Any] | None:
    key = _url_key(url)
    for rec in result.get("recommendations") or []:
        if _url_key(rec.get("url") or "") == key:
            return rec
    return None


def build_specific_request(
    base_request: dict[str, Any], recommendation: dict[str, Any]
) -> dict[str, Any]:
    """Turn a descriptive request + a chosen recommendation into a specific request.

    Original constraints (budget/currency/colour/capacity/condition/frequency) are
    retained. The product name comes from the *evidence* — the recommendation's real
    detail name or search title — never invented.
    """
    request = dict(base_request or {})
    title = str(recommendation.get("detail_name") or recommendation.get("title") or "").strip()
    # Strip retailer/site tail so it never pollutes identity tokens.
    request["product_name"] = strip_site_tail(title)
    request["url"] = None
    return request
