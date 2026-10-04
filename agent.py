"""LLM request parsing. Parsing never executes monitoring tasks."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from schemas import validate_price_extraction, validate_search_request, validate_task
import config


logger = logging.getLogger(__name__)


class ParserError(RuntimeError):
    """Safe configuration, provider or response error."""


# Bounded attempts for transient empty/truncated model responses only.
MAX_PARSE_ATTEMPTS = 2
MAX_RESPONSE_TOKENS = 4096

PROMPT_MONITOR = "parse_instruction.md"
PROMPT_SEARCH = "parse_shopping_request.md"
PROMPT_SUMMARISE_DEAL = "summarise_deal.md"
PROMPT_EXPLAIN_RESULT = "explain_result.md"
PROMPT_MATCH_ASPECT = "match_product_aspect.md"


def parse_user_instruction(user_instruction: str, *, client=None, model: str | None = None) -> dict:
    """Parse a monitoring request into schema v1 (a confirmed-task shape).

    Validation failures raise immediately; truncated or empty responses are retried
    a bounded number of times. Parsing never creates or starts a task.
    """
    return _parse_with_prompt(
        user_instruction,
        prompt_name=PROMPT_MONITOR,
        validator=validate_task,
        client=client,
        model=model,
        empty_message="Please enter a monitoring request.",
        invalid_message="Model response failed validation; no task was created.",
    )


def parse_shopping_request(user_instruction: str, *, client=None, model: str | None = None) -> dict:
    """Parse a *search* request into schema v2 (Stage B).

    Deliberately a different structure from a monitor task: a search is not
    permission to monitor anything, and it does not require a URL.
    """
    return _parse_with_prompt(
        user_instruction,
        prompt_name=PROMPT_SEARCH,
        validator=validate_search_request,
        client=client,
        model=model,
        empty_message="Please enter what you are looking for.",
        invalid_message="Model response failed validation; no search was started.",
    )


def _parse_with_prompt(
    user_instruction: str,
    *,
    prompt_name: str,
    validator,
    client=None,
    model: str | None = None,
    empty_message: str,
    invalid_message: str,
) -> dict:
    """Shared model call + validation path for both request schemas."""
    if not isinstance(user_instruction, str) or not user_instruction.strip():
        raise ParserError(empty_message)
    if len(user_instruction) > 8000:
        raise ParserError("Request exceeds 8000 characters.")
    if client is None:
        try:
            from llm_client import OpenAI
        except ImportError as exc:
            raise ParserError("Install dependencies with pip install -r requirements.txt.") from exc
        key = config.llm_api_key()
        base_url = config.llm_base_url()
        model = model or config.llm_model()
        if not model:
            raise ParserError("A model ID is required.")
        client = OpenAI(api_key=key, base_url=base_url, timeout=config.llm_timeout(), max_retries=1)
    if not model:
        raise ParserError("A model ID is required.")
    prompt_path = Path(__file__).with_name("prompts").joinpath(prompt_name)
    prompt = prompt_path.read_text(encoding="utf-8")

    last_error = "Empty or incomplete model response; please retry."
    for _attempt in range(MAX_PARSE_ATTEMPTS):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_instruction},
                ],
                response_format={"type": "json_object"},
                max_tokens=MAX_RESPONSE_TOKENS,
            )
            choice = response.choices[0]
            if choice.finish_reason == "length":
                last_error = "Model response was truncated; increase max_tokens or retry."
                continue
            if not choice.message.content:
                last_error = "Empty or incomplete model response; please retry."
                continue
            return validator(json.loads(choice.message.content), user_instruction)
        except ParserError:
            raise
        except (ValueError, TypeError, AttributeError, IndexError):
            raise ParserError(invalid_message) from None
        except Exception:
            raise ParserError("LLM request failed. Check configuration, quota and connectivity.") from None
    raise ParserError(last_error)


def summarise_parsed_task(task: dict) -> str:
    """Human-readable summary of a parsed task for CLI confirmation."""
    lines = [
        f"Product: {task.get('product_name')}",
        f"URL: {task.get('url')}",
        f"Target: {task.get('comparison')} {task.get('target_price')} {task.get('currency')}",
        f"Frequency: {task.get('frequency_minutes')} minutes",
    ]
    if task.get("condition"):
        lines.append(f"Condition: {task['condition']}")
    return "\n".join(lines)


class PriceExtractionError(RuntimeError):
    """A web page could not be turned into a validated price."""


def _make_client(model=None):
    """Build the OpenAI-compatible client once, resolving key/model from config."""
    try:
        from llm_client import OpenAI
    except ImportError as exc:
        raise ParserError("Install dependencies with pip install -r requirements.txt.") from exc
    key = config.llm_api_key()
    base_url = config.llm_base_url()
    model = model or config.llm_model()
    if not model:
        raise ParserError("A model ID is required.")
    client = OpenAI(api_key=key, base_url=base_url, timeout=config.llm_timeout(), max_retries=1)
    return client, model


def extract_price_from_page_text(page_text: str, *, client=None, model=None) -> dict:
    """Extract a product price from one web page's visible text using the LLM.

    The page text is untrusted data; the prompt tells the model to ignore any
    instruction-looking content inside it. Output is validated locally via
    ``validate_price_extraction``, so an invented or unparseable price fails closed.
    """
    if not isinstance(page_text, str) or not page_text.strip():
        raise PriceExtractionError("Page text is empty.")
    if client is None:
        client, model = _make_client(model)
    else:
        model = model or config.llm_model()
    if not model:
        raise PriceExtractionError("A model ID is required.")
    prompt_path = Path(__file__).with_name("prompts").joinpath("extract_price_from_page.md")
    prompt = prompt_path.read_text(encoding="utf-8")

    last_error = "Empty or incomplete model response; please retry."
    for _attempt in range(MAX_PARSE_ATTEMPTS):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": page_text},
                ],
                response_format={"type": "json_object"},
                max_tokens=MAX_RESPONSE_TOKENS,
            )
            choice = response.choices[0]
            if choice.finish_reason == "length":
                last_error = "Model response was truncated; increase max_tokens or retry."
                continue
            if not choice.message.content:
                last_error = "Empty or incomplete model response; please retry."
                continue
            return validate_price_extraction(json.loads(choice.message.content))
        except PriceExtractionError:
            raise
        except (ValueError, TypeError, AttributeError, IndexError):
            raise PriceExtractionError("Model response failed validation.") from None
        except Exception as exc:
            raise PriceExtractionError(
                f"LLM request failed: {type(exc).__name__}. Check configuration, quota and connectivity."
            ) from None
    raise PriceExtractionError(last_error)

def _normalise_query_key(text: str) -> str:
    """Casefold a query and collapse internal whitespace for dedupe comparisons."""
    return " ".join(text.split()).casefold()


def _is_finite_score(value: Any) -> bool:
    """True only for a finite real number (rejects bool, str, None, NaN and +/-inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def expand_search_queries(request: dict, *, client=None, model=None) -> list[str]:
    """Expand a natural-language shopping request into 3-5 Brave search queries.

    The LLM rewrites the request into short, specific search phrases that a web
    search engine would find product pages for. The original query is always kept as
    the first query; expanded queries are appended after deduplication. A failure
    returns just the original query — the caller never loses the ability to search.
    """
    query = str(request.get("query") or request.get("product_name") or "").strip()
    if not query:
        return []
    if client is None:
        try:
            client, model = _make_client(model)
        except Exception:
            return [query]
    else:
        model = model or config.llm_model()
    if not model:
        return [query]
    prompt_path = Path(__file__).with_name("prompts").joinpath("expand_search_queries.md")
    prompt = prompt_path.read_text(encoding="utf-8")
    prompt = prompt + (
        "\n\nRespond with a single JSON object of the form "
        '{"queries": ["<query>", "..."]} and no other text. '
        "The queries value must be an array of 3-5 short search strings."
    )
    user_text = json.dumps(
        {k: v for k, v in request.items() if k in ("query", "product_name", "colour", "capacity", "budget", "currency")},
        ensure_ascii=False,
    )
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_text},
            ],
            response_format={"type": "json_object"},
            max_tokens=MAX_RESPONSE_TOKENS,
        )
        choice = response.choices[0]
        if choice.finish_reason == "length" or not choice.message.content:
            return [query]
        data = json.loads(choice.message.content)
        # Accept the current object shape {"queries": [...]} or a legacy bare array.
        if isinstance(data, list):
            raw_queries = data
        elif isinstance(data, dict) and isinstance(data.get("queries"), list):
            raw_queries = data["queries"]
        else:
            return [query]
        # Original query first, then deduplicated expansions.
        original = query[:200]
        result = [original]
        seen = {_normalise_query_key(original)}
        for item in raw_queries:
            if not isinstance(item, str):
                continue
            candidate = item.strip()
            if not candidate:
                continue
            candidate = candidate[:200]
            key = _normalise_query_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            result.append(candidate)
            if len(result) >= 6:
                break
        return result
    except Exception:
        logger.debug("Search query expansion failed; using original query only.", exc_info=True)
        return [query]


def rank_candidates(
    candidates: list[dict],
    request: dict,
    *,
    client=None,
    model=None,
) -> list[dict]:
    """Re-rank candidates by LLM relevance to the request, preserving the original order as a tie-break.

    Only re-orders the list; it never removes candidates. A failure returns the input
    unchanged. The LLM scores each candidate title 0-10 for relevance to the request;
    ties keep the original position (stable sort).
    """
    if len(candidates) <= 3:
        return candidates
    if client is None:
        try:
            client, model = _make_client(model)
        except Exception:
            return candidates
    else:
        model = model or config.llm_model()
    if not model:
        return candidates
    query = str(request.get("query") or request.get("product_name") or "").strip()
    colour = str(request.get("colour") or "").strip()
    capacity = str(request.get("capacity") or "").strip()
    parts = [f"Request: {query}"]
    if colour:
        parts.append(f"colour={colour}")
    if capacity:
        parts.append(f"capacity={capacity}")
    titles = [str(c.get("title") or "") for c in candidates]
    user_text = "\n".join([*parts, "", "Candidates (title only):"] + [f"{i+1}. {t}" for i, t in enumerate(titles)])
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "Score each candidate title 0-10 for how likely it is the exact product the user wants. "
                    "Return ONLY a JSON object of the form {\"scores\": [...]}, no other text. "
                    "scores must be numbers 0-10, in the same order and the same length as the input list. "
                    "10=exact match, 0=completely different product."
                )},
                {"role": "user", "content": user_text},
            ],
            response_format={"type": "json_object"},
            max_tokens=MAX_RESPONSE_TOKENS,
        )
        choice = response.choices[0]
        if choice.finish_reason == "length" or not choice.message.content:
            return candidates
        data = json.loads(choice.message.content)
        # Accept the current object shape {"scores": [...]} or a legacy bare array.
        if isinstance(data, list):
            scores = data
        elif isinstance(data, dict) and isinstance(data.get("scores"), list):
            scores = data["scores"]
        else:
            return candidates
        if len(scores) != len(candidates):
            return candidates
        if not all(_is_finite_score(s) for s in scores):
            return candidates
        # Stable sort by score descending; ties keep original order.
        scored = list(zip(scores, range(len(candidates)), candidates))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [c for _, _, c in scored]
    except Exception:
        logger.debug("Candidate ranking failed; keeping original order.", exc_info=True)
        return candidates


def summarise_deal(task: dict, quote: dict, *, client=None, model=None) -> str:
    """Generate a short natural-language summary of a price-alert for an email.

    This is an *enhancement*, never a gate: a failure here must not prevent the
    alert from being sent. The caller owns the deterministic fallback. Output is
    bounded and treated as plain text (never trusted as instructions); the prompt
    tells the model the task/quote are DATA.
    """
    if client is None:
        client, model = _make_client(model)
    else:
        model = model or config.llm_model()
    if not model:
        raise ParserError("A model ID is required.")
    prompt_path = Path(__file__).with_name("prompts").joinpath(PROMPT_SUMMARISE_DEAL)
    prompt = prompt_path.read_text(encoding="utf-8")
    payload = {
        "product_name": task.get("product_name"),
        "target": f"{task.get('comparison')} {task.get('currency')} {task.get('target_price')}",
        "best": {
            "retailer": quote.get("retailer"),
            "price": quote.get("price"),
            "currency": quote.get("currency"),
            "in_stock": quote.get("in_stock"),
        },
        "other_sources": quote.get("other_sources") or [],
    }
    user_content = json.dumps(payload, ensure_ascii=False)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=MAX_RESPONSE_TOKENS,
        )
    except Exception as exc:
        raise ParserError(f"LLM request failed: {type(exc).__name__}.") from None
    choice = response.choices[0]
    text = (choice.message.content or "").strip()
    if not text:
        raise ParserError("Empty model response.")
    if len(text) > 2000:
        text = text[:2000].rstrip()
    return text


def explain_result(kind, reasons=None, *, client=None, model=None):
    """Optional plain-English explanation for a non-confirmed or unreadable result.

    Pure display enhancement: it never changes a match verdict, never makes an offer
    selectable, and a failure falls back to the caller's deterministic rendering.
    `kind` is `needs_confirmation` or `unavailable` only.
    """
    if kind not in ("needs_confirmation", "unavailable"):
        raise ParserError(f"Unsupported explanation kind: {kind!r}.")
    if client is None:
        client, model = _make_client(model)
    else:
        model = model or config.llm_model()
    if not model:
        raise ParserError("A model ID is required.")
    prompt_path = Path(__file__).with_name("prompts").joinpath(PROMPT_EXPLAIN_RESULT)
    prompt = prompt_path.read_text(encoding="utf-8")
    payload = {"kind": kind, "reasons": [str(r) for r in (reasons or [])]}
    user_content = json.dumps(payload, ensure_ascii=False)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=MAX_RESPONSE_TOKENS,
        )
    except Exception as exc:
        raise ParserError(f"LLM request failed: {type(exc).__name__}.") from None
    choice = response.choices[0]
    text = (choice.message.content or "").strip()
    if not text:
        raise ParserError("Empty model response.")
    if len(text) > 300:
        text = text[:300].rstrip()
    return text


def match_product_aspect(aspect, request_value, candidate_title, query, *, client=None, model=None):
    """Ask the LLM to judge one product aspect (colour or accessory).

    Returns ``(verdict, reason)`` where verdict is ``same``/``different``/``needs_confirmation``.
    A failure raises ``ParserError`` so the caller falls back to the deterministic logic.
    This is an enhancement, never a gate: it only runs when the deterministic logic
    cannot decide, and it never overrides a deterministic ``different``.
    """
    if aspect not in ("colour", "accessory", "concreteness", "identity"):
        raise ParserError(f"Unsupported aspect: {aspect!r}.")
    if not candidate_title or not str(candidate_title).strip():
        raise ParserError("Candidate title is required.")
    if client is None:
        # A network client is only created when the AI judge is enabled and the
        # active credential came from the process environment, not merely from
        # the operator's .env file: an offline test that injects a placeholder
        # key through patch.dict(os.environ) would otherwise have its fake key
        # shadowed by the real .env value and silently reach the network.
        if not config.llm_concreteness():
            raise ParserError("LLM concreteness judgement is disabled.")
        if not config.from_process_env("LLM_API_KEY"):
            raise ParserError("No process-level LLM credential for the AI judge.")
        client, model = _make_client(model)
    else:
        model = model or config.llm_model()
    if not model:
        raise ParserError("A model ID is required.")
    prompt_path = Path(__file__).with_name("prompts").joinpath(PROMPT_MATCH_ASPECT)
    prompt = prompt_path.read_text(encoding="utf-8")
    payload = {
        "aspect": aspect,
        "request_value": request_value,
        "candidate_title": str(candidate_title).strip(),
        "query": str(query or "").strip(),
    }
    user_content = json.dumps(payload, ensure_ascii=False)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            max_tokens=MAX_RESPONSE_TOKENS,
        )
    except Exception as exc:
        raise ParserError(f"LLM request failed: {type(exc).__name__}.") from None
    choice = response.choices[0]
    content = (choice.message.content or "").strip()
    if not content:
        raise ParserError("Empty model response.")
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        raise ParserError("Model response was not valid JSON.") from None
    verdict = str(data.get("verdict") or "").strip()
    reason = str(data.get("reason") or "").strip()
    if verdict not in ("same", "different", "needs_confirmation"):
        raise ParserError(f"Invalid verdict: {verdict!r}.")
    if not reason:
        raise ParserError("Missing reason.")
    return verdict, reason
