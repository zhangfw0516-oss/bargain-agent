"""Linear 5-stage AI find pipeline (intelligent-find mode).

User flow implemented here (the bounded, AI-driven part):

    free-text description
      -> [1] Understand AI parses it into a structured need
      -> [2] Query-rewrite AI turns the need into 2-4 web-search queries
      -> [3] plain code runs the searches (Brave + Tavily merged) and reads the
             top detail pages (SSRF-protected) -- no AI in this step
      -> [4] Decision AI picks the concrete products from the observed evidence
      -> [5] Describe AI writes a short Chinese summary for each recommendation
      -> the user confirms an actual product, which turns the descriptive request
         into a *specific* product request
      -> the existing deterministic same-product comparison and monitor
         confirmation then run unchanged (see ``discovery``/``webapp``).

Design rules (all enforced by code, not by the models):

- **One bounded pass.** There is no "what next" loop: searches and detail fetches
  are decided by code against fixed limits, so cost is predictable (four LLM
  calls per run: understand / rewrite / select / describe).
- **Observed-evidence-only selection.** The decision model may only reference
  ``provenance_id`` values that a real search returned; invented ids are dropped.
- **No invented prices or URLs.** A price only enters evidence from a real detail
  page; a candidate URL only enters evidence from a real search result.
- **Per-stage deterministic fallback.** Every AI stage degrades to plain code on
  failure (bad JSON, timeout, provider error), so a result is always produced and
  the honest reason is recorded in ``notes``.
- **Budget / safety vetoes stay in code.** Over-budget prices, non-http(s) URLs
  and search-operator injection are rejected here, never delegated to a model.
- **No task creation.** Nothing here writes to the database. A monitoring task is
  only ever created by the existing final confirmation step.

Every external collaborator (model, search, fetch, clock) is injectable, so the
whole flow is tested offline with no network, LLM or SMTP.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

import iterative_discovery as itd

logger = logging.getLogger(__name__)

PROMPT_UNDERSTAND = "find_understand.md"
PROMPT_REWRITE = "find_rewrite.md"
PROMPT_SELECT = "find_select.md"
PROMPT_DESCRIBE = "find_describe.md"

MAX_REWRITE_QUERIES = 4
MAX_SUMMARY_ITEMS = 8


# --------------------------------------------------------------------------- helpers


def _read_prompt(name: str) -> str:
    return Path(__file__).with_name("prompts").joinpath(name).read_text(encoding="utf-8")


def _llm_call(prompt_name: str, payload: dict[str, Any], *, client=None, model: str | None = None) -> Any:
    """One bounded LLM call returning parsed JSON. Raises on any failure.

    The caller (a pipeline stage) treats any exception as "this AI stage failed"
    and falls back to deterministic code. ``response_format=json_object`` plus a
    strict local parse keep the model honest.
    """
    if client is None:
        client, model = itd._make_client(model)
    if not model:
        raise itd.DiscoveryError("A model ID is required.")
    messages = [
        {"role": "system", "content": _read_prompt(prompt_name)},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        max_tokens=itd.MAX_RESPONSE_TOKENS,
    )
    content = response.choices[0].message.content
    if not content:
        raise itd.DiscoveryError("The model returned an empty answer.")
    try:
        return json.loads(content)
    except (ValueError, TypeError) as exc:
        raise itd.DiscoveryError("The model answer was not valid JSON.") from None


# --------------------------------------------------------------------------- stage 1: understand


_UNDERSTAND_FIELDS = ("category", "key_specs", "budget", "currency", "colour", "use_case", "exclusions")


def _clean_understanding(raw: Any) -> dict[str, Any] | None:
    """Normalise the understand-stage JSON, or None when unusable."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    category = raw.get("category")
    if isinstance(category, str) and category.strip():
        out["category"] = " ".join(category.split())[:120]
    specs = raw.get("key_specs")
    if isinstance(specs, list):
        out["key_specs"] = [" ".join(str(s).split())[:60] for s in specs if isinstance(s, str) and s.strip()][:8]
    budget = raw.get("budget")
    if budget not in (None, ""):
        try:
            from decimal import Decimal

            out["budget"] = str(Decimal(str(budget)))
        except Exception:
            pass
    currency = raw.get("currency")
    if isinstance(currency, str) and currency.strip():
        out["currency"] = currency.strip().upper()[:8]
    colour = raw.get("colour")
    if isinstance(colour, str) and colour.strip():
        out["colour"] = " ".join(colour.split())[:40]
    use_case = raw.get("use_case")
    if isinstance(use_case, str) and use_case.strip():
        out["use_case"] = " ".join(use_case.split())[:80]
    exclusions = raw.get("exclusions")
    if isinstance(exclusions, list):
        out["exclusions"] = [" ".join(str(s).split())[:60] for s in exclusions if isinstance(s, str) and s.strip()][:8]
    return out or None


def run_understand(
    description: str,
    *,
    understand_fn: Callable[[str], dict[str, Any] | None] | None = None,
    client=None,
    model: str | None = None,
    notes: list[str],
) -> dict[str, Any] | None:
    """Stage 1: parse the free-text description into a structured need.

    Fallback: return None (the pipeline then uses the raw description as the
    single query). Never raises for a model failure.
    """
    if understand_fn is not None:
        try:
            return _clean_understanding(understand_fn(description))
        except Exception as exc:
            notes.append(f"Understand stage failed: {type(exc).__name__}.")
            logger.warning("Understand stage failed: %s", exc)
            return None
    try:
        parsed = _llm_call(PROMPT_UNDERSTAND, {"description": description}, client=client, model=model)
    except Exception as exc:
        notes.append(f"Understand stage fell back to the raw description ({type(exc).__name__}).")
        logger.warning("Understand stage failed: %s", exc)
        return None
    cleaned = _clean_understanding(parsed)
    if cleaned is None:
        notes.append("Understand stage returned no usable structure; using the raw description.")
    return cleaned


# --------------------------------------------------------------------------- stage 2: rewrite


def _clean_queries(raw: Any) -> list[str]:
    if not isinstance(raw, dict):
        return []
    queries = raw.get("queries")
    if not isinstance(queries, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in queries:
        if not isinstance(item, str):
            continue
        q = " ".join(item.split())
        if not q or len(q) > itd.MAX_QUERY_CHARS:
            continue
        # Reuse the Brave operator rejection so a rewritten query can never
        # smuggle search operators into the request.
        try:
            q = itd._default_sanitise_query(q)
        except Exception:
            continue
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= MAX_REWRITE_QUERIES:
            break
    return out


def _fallback_queries(description: str, understanding: dict[str, Any] | None) -> list[str]:
    """Deterministic query when the rewrite AI fails."""
    if understanding and understanding.get("category"):
        parts = [understanding["category"]]
        if understanding.get("colour"):
            parts.append(understanding["colour"])
        if understanding.get("use_case"):
            parts.append(understanding["use_case"])
        q = " ".join(parts)
        try:
            return [itd._default_sanitise_query(q)]
        except Exception:
            pass
    q = " ".join(str(description or "").split())[: itd.MAX_QUERY_CHARS]
    try:
        return [itd._default_sanitise_query(q)] if q else []
    except Exception:
        return [q] if q else []


def run_rewrite(
    description: str,
    understanding: dict[str, Any] | None,
    *,
    rewrite_fn: Callable[[dict[str, Any]], list[str]] | None = None,
    client=None,
    model: str | None = None,
    notes: list[str],
) -> list[str]:
    """Stage 2: turn the structured need into 2-4 search queries.

    Fallback: category+specs, then the raw description. Never raises.
    """
    payload = {"description": description, "need": understanding or {}}
    if rewrite_fn is not None:
        try:
            queries = _clean_queries({"queries": rewrite_fn(payload)})
        except Exception as exc:
            notes.append(f"Rewrite stage failed: {type(exc).__name__}.")
            logger.warning("Rewrite stage failed: %s", exc)
            queries = []
    else:
        try:
            queries = _clean_queries(_llm_call(PROMPT_REWRITE, payload, client=client, model=model))
        except Exception as exc:
            notes.append(f"Rewrite stage fell back to a deterministic query ({type(exc).__name__}).")
            logger.warning("Rewrite stage failed: %s", exc)
            queries = []
    if not queries:
        queries = _fallback_queries(description, understanding)
    if not queries:
        notes.append("No search query could be built from the description.")
    return queries


# --------------------------------------------------------------------------- stage 3: search + fetch (code only)


def run_search_and_fetch(
    queries: list[str],
    *,
    search_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    fetch_fn: Callable[[str], dict[str, Any]] | None = None,
    limits: itd.DiscoveryLimits,
    clock: Callable[[], float],
    started: float,
    notes: list[str],
) -> tuple[dict[str, dict[str, Any]], list[str], int, int]:
    """Stage 3: run every query against the merged web search, then read the top
    detail pages. No AI here; limits come straight from ``DiscoveryLimits``.

    Returns ``(evidence, order, searches_used, fetches_used)``.
    """
    do_search = search_fn or _merged_search
    do_fetch = fetch_fn or itd._default_fetch

    evidence: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    url_to_pid: dict[str, str] = {}
    next_id = 1
    searches = 0
    fetches = 0

    for query in queries:
        if searches >= limits.max_searches:
            notes.append(f"The search budget ({limits.max_searches}) was reached.")
            break
        if clock() - started >= limits.deadline_seconds:
            notes.append("The deadline was reached during searching.")
            break
        searches += 1
        logger.info("Find: search %d/%d - %s", searches, min(len(queries), limits.max_searches), query)
        try:
            results = do_search(query)
        except Exception as exc:
            notes.append(f"Search {query!r} failed: {type(exc).__name__}.")
            logger.warning("Find search failed: %s", exc)
            continue
        added = itd._add_search_candidates(
            results,
            query=query,
            round_no=searches,
            next_id=next_id,
            evidence=evidence,
            order=order,
            url_to_pid=url_to_pid,
        )
        next_id += added
        logger.info("Find: search returned %d candidate(s), %d new.", len(results or []), added)

    # Read the top detail pages (priced products need a real page). Candidates are
    # read in observed order up to the detail-fetch budget and the deadline.
    for pid in list(order):
        if fetches >= limits.max_detail_fetches:
            break
        if clock() - started >= limits.deadline_seconds:
            notes.append("The deadline was reached while reading product pages.")
            break
        url = evidence[pid]["url"]
        fetches += 1
        logger.info("Find: reading product page %d/%d: %s", fetches, limits.max_detail_fetches, url)
        try:
            quote = do_fetch(url)
        except Exception as exc:
            evidence[pid]["fetch_error"] = f"{type(exc).__name__}: {exc}"
            notes.append(f"Detail page for {evidence[pid]['title']!r} could not be read.")
            continue
        detail = itd._clean_detail(quote)
        if detail is None:
            evidence[pid]["fetch_error"] = "The page did not state a product name."
        else:
            evidence[pid]["detail"] = detail
            evidence[pid]["fetch_error"] = None

    return evidence, order, searches, fetches


def _merged_search(query: str) -> list[dict[str, Any]]:
    """Brave + Tavily merged candidates (de-duplicated by URL), one query."""
    import discovery as disc

    return disc._default_web_candidates(query)


# --------------------------------------------------------------------------- stage 4: select


def run_select(
    description: str,
    understanding: dict[str, Any] | None,
    evidence: dict[str, dict[str, Any]],
    order: list[str],
    *,
    select_fn: Callable[[dict[str, Any]], list[str]] | None = None,
    client=None,
    model: str | None = None,
    notes: list[str],
) -> list[str]:
    """Stage 4: the decision AI picks provenance ids from observed evidence.

    Returns an ordered list of *observed* ids (invented/unknown ids dropped).
    Fallback: empty list (the recommendation builder then uses insertion order).
    """
    payload = {
        "description": description,
        "need": understanding or {},
        "evidence": [itd._evidence_summary(evidence[pid]) for pid in order],
    }
    raw: Any = None
    if select_fn is not None:
        try:
            raw = {"candidates": select_fn(payload)}
        except Exception as exc:
            notes.append(f"Select stage failed: {type(exc).__name__}.")
            logger.warning("Select stage failed: %s", exc)
            raw = None
    else:
        try:
            raw = _llm_call(PROMPT_SELECT, payload, client=client, model=model)
        except Exception as exc:
            notes.append(f"Select stage fell back to evidence order ({type(exc).__name__}).")
            logger.warning("Select stage failed: %s", exc)
            raw = None
    if not isinstance(raw, dict):
        return []
    candidates = raw.get("candidates")
    if not isinstance(candidates, list):
        return []
    picked: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if isinstance(item, str):
            pid = item.strip()
            if pid in evidence and pid not in seen:
                seen.add(pid)
                picked.append(pid)
    dropped = len([i for i in candidates if isinstance(i, str)]) - len(picked)
    if dropped:
        notes.append(f"Select stage referenced {dropped} unknown evidence id(s); they were ignored.")
    return picked


# --------------------------------------------------------------------------- stage 5: describe


def run_describe(
    description: str,
    understanding: dict[str, Any] | None,
    recommendations: list[dict[str, Any]],
    *,
    describe_fn: Callable[[dict[str, Any]], dict[str, str]] | None = None,
    client=None,
    model: str | None = None,
    notes: list[str],
) -> dict[str, str]:
    """Stage 5: one batched LLM call writes a Chinese summary per recommendation.

    Fallback: empty dict (the UI then shows the structured fields only).
    """
    items = [
        {
            "provenance_id": r.get("provenance_id"),
            "title": r.get("title"),
            "price": r.get("price"),
            "currency": r.get("currency"),
            "in_stock": r.get("in_stock"),
            "source": r.get("source"),
        }
        for r in (recommendations or [])[:MAX_SUMMARY_ITEMS]
        if r.get("provenance_id")
    ]
    if not items:
        return {}
    payload = {"description": description, "need": understanding or {}, "products": items}
    raw: Any = None
    if describe_fn is not None:
        try:
            raw = {"summaries": describe_fn(payload)}
        except Exception as exc:
            notes.append(f"Describe stage failed: {type(exc).__name__}.")
            logger.warning("Describe stage failed: %s", exc)
            raw = None
    else:
        try:
            raw = _llm_call(PROMPT_DESCRIBE, payload, client=client, model=model)
        except Exception as exc:
            notes.append(f"Describe stage skipped ({type(exc).__name__}); showing structured fields only.")
            logger.warning("Describe stage failed: %s", exc)
            raw = None
    if not isinstance(raw, dict):
        return {}
    summaries = raw.get("summaries")
    if not isinstance(summaries, dict):
        return {}
    valid_ids = {item["provenance_id"] for item in items}
    out: dict[str, str] = {}
    for pid, text in summaries.items():
        if pid in valid_ids and isinstance(text, str) and text.strip():
            out[pid] = text.strip()[:600]
    return out


# --------------------------------------------------------------------------- orchestrator


def run_find_pipeline(
    description: str,
    *,
    understand_fn: Callable[[str], dict[str, Any] | None] | None = None,
    rewrite_fn: Callable[[dict[str, Any]], list[str]] | None = None,
    select_fn: Callable[[dict[str, Any]], list[str]] | None = None,
    describe_fn: Callable[[dict[str, Any]], dict[str, str]] | None = None,
    search_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    fetch_fn: Callable[[str], dict[str, Any]] | None = None,
    limits: itd.DiscoveryLimits | None = None,
    request: dict[str, Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run the linear 5-stage pipeline and return recommendations + evidence.

    Returns the same shape as ``iterative_discovery.run_iterative_discovery``,
    plus an optional ``summaries`` map and a per-stage ``stages`` trace. Never
    raises for a model/tool failure; those become honest notes on the result.
    """
    description = str(description or "").strip()
    limits = limits or itd.DiscoveryLimits.from_env()
    constraints = dict(request or {})
    notes: list[str] = []
    stages: dict[str, str] = {}
    started = clock()

    # 1 - understand
    logger.info("Find pipeline: stage 1/5 - understanding the description...")
    understanding = run_understand(
        description, understand_fn=understand_fn, notes=notes
    )
    stages["understand"] = "ok" if understanding is not None else "fallback"

    # 2 - rewrite
    logger.info("Find pipeline: stage 2/5 - rewriting search queries...")
    queries = run_rewrite(
        description, understanding, rewrite_fn=rewrite_fn, notes=notes
    )
    stages["rewrite"] = "ok" if queries and not _fallback_used(description, understanding, queries) else "ok"
    if not queries:
        stages["rewrite"] = "empty"

    # 3 - search + fetch
    logger.info("Find pipeline: stage 3/5 - searching and reading product pages...")
    evidence, order, searches, fetches = run_search_and_fetch(
        queries,
        search_fn=search_fn,
        fetch_fn=fetch_fn,
        limits=limits,
        clock=clock,
        started=started,
        notes=notes,
    )
    stages["search_fetch"] = "ok" if order else "empty"

    # 4 - select
    logger.info("Find pipeline: stage 4/5 - selecting concrete products...")
    model_order = run_select(
        description, understanding, evidence, order, select_fn=select_fn, notes=notes
    )
    stages["select"] = "ok" if model_order else "fallback"

    # Build recommendations with the existing deterministic gates (concreteness,
    # budget veto, priced-first ordering, model order applied first).
    recommendations = itd._recommendations(
        evidence, order, limits.max_recommendations, model_order,
        description=description, constraints=constraints,
    )

    # 5 - describe
    logger.info("Find pipeline: stage 5/5 - writing product summaries...")
    summaries = run_describe(
        description, understanding, recommendations, describe_fn=describe_fn, notes=notes
    )
    stages["describe"] = "ok" if summaries else "fallback"
    for rec in recommendations:
        pid = rec.get("provenance_id")
        if pid and pid in summaries:
            rec["summary"] = summaries[pid]

    elapsed = max(0.0, clock() - started)
    if not order:
        finished_reason = "no_evidence"
    elif clock() - started >= limits.deadline_seconds:
        finished_reason = itd.FINISHED_DEADLINE
    else:
        finished_reason = "pipeline_complete"
    status = itd.STATUS_READY if recommendations else itd.STATUS_PARTIAL
    if not recommendations:
        notes.append("No concrete product could be recommended from the evidence gathered.")

    return {
        "status": status,
        "ok": status == itd.STATUS_READY,
        "description": description,
        "request": constraints,
        "recommendations": recommendations,
        "summaries": summaries,
        "evidence": [evidence[pid] for pid in order],
        "notes": notes,
        "stages": stages,
        "progress": {
            "rounds": 1,
            "searches": searches,
            "detail_fetches": fetches,
            "evidence": len(order),
            "finished_reason": finished_reason,
            "elapsed_seconds": round(elapsed, 3),
            "limits": limits.as_dict(),
        },
    }


def _fallback_used(description: str, understanding: dict[str, Any] | None, queries: list[str]) -> bool:
    """Best-effort check whether the rewrite fell back to a deterministic query."""
    return queries == _fallback_queries(description, understanding)
