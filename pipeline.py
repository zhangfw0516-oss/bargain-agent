"""The Find pipeline: a LangGraph of AI agents around one code-only search step.

    understand (AI) -> rewrite (AI) -> search (code) -> select (AI) -> describe (AI)
                          ^                                 |
                          +---- nothing suitable, retry ----+

- **Search is code.** SerpApi Google Shopping supplies structured listings with
  prices; Firecrawl adds candidates only when SerpApi returns too few, and their
  prices must come from the price-extractor agent reading the returned page.
- **Agents choose, code decides.** The select agent may only name evidence ids the
  server assigned; budget and condition vetoes are re-applied in code.
- **Every AI node degrades.** A model failure becomes a deterministic fallback and
  a ``fallback`` stage status, so a result is always produced without an LLM.
- **Bounded cost.** Query count, deadline, rewrite rounds and supplementary page
  extractions are limited by configuration.
- **No side effects.** Nothing here writes to the database or sends email.
"""

from __future__ import annotations

import logging
import operator
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Callable, TypedDict

os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")

from langgraph.graph import END, START, StateGraph  # noqa: E402

import config  # noqa: E402
from agents import FALLBACK, OK, SKIPPED, LLMOptions  # noqa: E402
from agents import describe as describe_agent  # noqa: E402
from agents import price_extractor  # noqa: E402
from agents import rewrite as rewrite_agent  # noqa: E402
from agents import select as select_agent  # noqa: E402
from agents import understand as understand_agent  # noqa: E402
from pricing import host_of, merchant_trust  # noqa: E402
from sources.base import ProviderError, ShoppingResult, ShoppingSearch  # noqa: E402

logger = logging.getLogger(__name__)

STATUS_READY = "ready"
STATUS_PARTIAL = "partial"


@dataclass
class FindLimits:
    max_queries: int = 3
    deadline_seconds: float = 120.0
    max_recommendations: int = 8
    max_rewrite_rounds: int = 2
    results_per_query: int = 20
    supplement_min_results: int = 5
    max_supplement_extractions: int = 3

    @classmethod
    def from_config(cls) -> "FindLimits":
        return cls(
            max_queries=config.find_max_queries(),
            deadline_seconds=float(config.find_deadline_seconds()),
            max_recommendations=config.find_max_recommendations(),
            supplement_min_results=config.supplement_min_results(),
        )


@dataclass
class FindDeps:
    shopping: ShoppingSearch | None = None
    supplement: ShoppingSearch | None = None
    limits: FindLimits = field(default_factory=FindLimits)
    clock: Callable[[], float] = time.monotonic
    #: Per-agent client/model override; tests return a FakeLLM here.
    llm_for: Callable[[str], LLMOptions] = lambda _agent: LLMOptions()
    #: Called with a node name as each stage starts (progress display).
    on_stage: Callable[[str], None] = lambda _stage: None


def _merge(left: dict, right: dict) -> dict:
    return {**(left or {}), **(right or {})}


class FindState(TypedDict, total=False):
    description: str
    #: The user's latest follow-up when refining an earlier find.
    followup: str | None
    constraints: dict[str, Any]
    started: float
    need: dict[str, Any]
    queries: list[str]
    all_queries: Annotated[list[str], operator.add]
    rewrite_rounds: int
    searches: int
    supplement_searches: int
    evidence: Annotated[dict[str, dict[str, Any]], _merge]
    selected: list[str]
    select_status: str
    select_note: str | None
    recommendations: list[dict[str, Any]]
    stages: Annotated[dict[str, str], _merge]
    notes: Annotated[list[str], operator.add]


# --------------------------------------------------------------------------- evidence


def evidence_from(result: ShoppingResult, *, eid: str, query: str) -> dict[str, Any]:
    merchant = result.merchant or (host_of(result.link) or None)
    return {
        "id": eid,
        "title": result.title,
        "link": result.link,
        "merchant": merchant,
        "price": result.price,
        "currency": result.currency,
        "price_source": result.provider if result.price else None,
        "product_id": result.product_id,
        "page_token": result.page_token,
        "thumbnail": result.thumbnail,
        "condition": result.condition,
        "provider": result.provider,
        "snippet": result.snippet,
        "query": query,
        "trusted": merchant_trust(merchant, result.link),
    }


def _dedupe_key(item: dict[str, Any]) -> str:
    return f"pid:{item['product_id']}" if item.get("product_id") else f"url:{item['link'].rstrip('/').lower()}"


def _to_decimal(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return amount if amount.is_finite() else None


def build_recommendations(
    evidence: dict[str, dict[str, Any]],
    order: list[str],
    need: dict[str, Any],
    constraints: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    """Apply the code-owned vetoes to the agent's order and cap the list."""
    budget = _to_decimal(constraints.get("budget") or need.get("budget"))
    budget_currency = constraints.get("currency") or need.get("currency")
    wanted_condition = need.get("condition")
    recs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for eid in order:
        item = evidence.get(eid)
        if item is None:
            continue
        key = _dedupe_key(item)
        if key in seen:
            continue
        price = _to_decimal(item.get("price"))
        same_currency = not budget_currency or not item.get("currency") or item["currency"] == budget_currency
        if budget is not None and price is not None and same_currency and price > budget:
            continue
        if item.get("condition") and item["condition"] != wanted_condition:
            continue
        seen.add(key)
        recs.append(
            {
                **{k: item.get(k) for k in (
                    "id", "title", "link", "merchant", "price", "currency", "price_source", "product_id",
                    "page_token", "thumbnail", "condition", "provider", "trusted",
                )},
                "search_query": item.get("query"),
                "monitorable": bool(item.get("product_id")),
            }
        )
        if len(recs) >= limit:
            break
    return recs


# --------------------------------------------------------------------------- graph


def build_graph(deps: FindDeps):
    limits = deps.limits

    def out_of_time(state: FindState) -> bool:
        return deps.clock() - state["started"] >= limits.deadline_seconds

    def understand_node(state: FindState) -> dict[str, Any]:
        result = understand_agent.run(state["description"], opts=deps.llm_for(understand_agent.NAME))
        return {"need": result.value, "stages": {"understand": result.status}, "notes": [result.note] if result.note else []}

    def rewrite_node(state: FindState) -> dict[str, Any]:
        rounds = state.get("rewrite_rounds", 0)
        remaining = limits.max_queries - state.get("searches", 0)
        retry = rounds > 0
        refining = bool(state.get("followup"))
        feedback = None
        if retry:
            feedback = state.get("select_note") or (
                "The previous queries found no suitable product." if state.get("evidence") else "The previous queries returned no listings."
            )
        elif refining:
            feedback = f"The user refined the request: {state['followup']}. Search for what the refinement adds."
        result = rewrite_agent.run(
            state["description"],
            state.get("need"),
            max_queries=max(1, remaining),
            previous_queries=state.get("all_queries") if (retry or refining) else None,
            feedback=feedback,
            opts=deps.llm_for(rewrite_agent.NAME),
        )
        stage = "rewrite_retry" if retry else "rewrite"
        return {
            "queries": result.value,
            "all_queries": result.value,
            "rewrite_rounds": rounds + 1,
            "stages": {stage: result.status if result.value else "empty"},
            "notes": [result.note] if result.note else [],
        }

    def search_node(state: FindState) -> dict[str, Any]:
        notes: list[str] = []
        searches = state.get("searches", 0)
        existing = dict(state.get("evidence") or {})
        keys = {_dedupe_key(e) for e in existing.values()}
        new: dict[str, dict[str, Any]] = {}

        def add(result: ShoppingResult, query: str) -> dict[str, Any] | None:
            eid = f"e{len(existing) + len(new) + 1}"
            item = evidence_from(result, eid=eid, query=query)
            key = _dedupe_key(item)
            if key in keys:
                return None
            keys.add(key)
            new[eid] = item
            return item

        if deps.shopping is None:
            notes.append("Google Shopping search is unavailable (SERPAPI_API_KEY not set).")
        batch: list[str] = []
        if deps.shopping is not None:
            if out_of_time(state):
                notes.append("The search deadline was reached.")
            else:
                batch = (state.get("queries") or [])[: max(0, limits.max_queries - searches)]
        searches += len(batch)

        def run_search(query: str):
            try:
                return deps.shopping.search(query, limit=limits.results_per_query), None
            except ProviderError as exc:
                return [], exc

        # Queries run in parallel; results are merged in query order, so evidence
        # ids stay deterministic.
        with ThreadPoolExecutor(max_workers=max(1, len(batch))) as pool:
            outcomes = list(pool.map(run_search, batch))
        for query, (results, error) in zip(batch, outcomes):
            if error is not None:
                notes.append(f"Search for {query!r} failed: {error}")
            for result in results:
                add(result, query)

        supplement_searches = state.get("supplement_searches", 0)
        if (
            deps.supplement is not None
            and supplement_searches == 0
            and len(existing) + len(new) < limits.supplement_min_results
            and state.get("queries")
            and not out_of_time(state)
        ):
            supplement_searches += 1
            query = state["queries"][0]
            try:
                results = deps.supplement.search(query, limit=5)
            except ProviderError as exc:
                notes.append(f"Supplementary search failed: {exc}")
                results = []
            extractions = 0
            for result in results:
                item = add(result, query)
                if item is None or not result.page_text or item.get("price"):
                    continue
                if extractions >= limits.max_supplement_extractions or out_of_time(state):
                    continue
                extractions += 1
                try:
                    extracted = price_extractor.run(result.page_text, url=result.link, opts=deps.llm_for(price_extractor.NAME))
                except price_extractor.PriceExtractionError:
                    continue
                item.update(price=extracted["price"], currency=extracted["currency"], price_source="page_extract")

        found = len(new)
        stage = OK if found else ("unavailable" if deps.shopping is None and deps.supplement is None else "empty")
        return {
            "evidence": new,
            "searches": searches,
            "supplement_searches": supplement_searches,
            "stages": {f"search_{state.get('rewrite_rounds', 1)}": stage},
            "notes": notes,
        }

    def select_node(state: FindState) -> dict[str, Any]:
        evidence = list((state.get("evidence") or {}).values())
        result = select_agent.run(state["description"], state.get("need"), evidence, opts=deps.llm_for(select_agent.NAME))
        return {
            "selected": result.value,
            "select_status": result.status,
            "select_note": result.note,
            "stages": {"select": result.status},
            "notes": [result.note] if result.note else [],
        }

    def after_select(state: FindState) -> str:
        if (
            state.get("select_status") == OK
            and not state.get("selected")
            and state.get("rewrite_rounds", 0) < limits.max_rewrite_rounds
            and state.get("searches", 0) < limits.max_queries
            and deps.shopping is not None
            and not out_of_time(state)
        ):
            return "rewrite"
        return "describe"

    def describe_node(state: FindState) -> dict[str, Any]:
        recs = build_recommendations(
            state.get("evidence") or {},
            state.get("selected") or [],
            state.get("need") or {},
            state.get("constraints") or {},
            limits.max_recommendations,
        )
        if not recs:
            return {"recommendations": [], "stages": {"describe": SKIPPED}}
        result = describe_agent.run(state["description"], state.get("need"), recs, opts=deps.llm_for(describe_agent.NAME))
        for rec in recs:
            rec["summary"] = result.value.get(rec["id"])
        return {"recommendations": recs, "stages": {"describe": result.status}, "notes": [result.note] if result.note else []}

    graph = StateGraph(FindState)
    def staged(name: str, node: Callable[[FindState], dict[str, Any]]):
        def run(state: FindState) -> dict[str, Any]:
            try:
                deps.on_stage(name)
            except Exception:  # noqa: BLE001 - progress reporting must never break a find
                logger.debug("on_stage callback failed", exc_info=True)
            return node(state)

        return run

    graph.add_node("understand", staged("understand", understand_node))
    graph.add_node("rewrite", staged("rewrite", rewrite_node))
    graph.add_node("search", staged("search", search_node))
    graph.add_node("select", staged("select", select_node))
    graph.add_node("describe", staged("describe", describe_node))
    graph.add_edge(START, "understand")
    graph.add_edge("understand", "rewrite")
    graph.add_edge("rewrite", "search")
    graph.add_edge("search", "select")
    graph.add_conditional_edges("select", after_select, {"rewrite": "rewrite", "describe": "describe"})
    graph.add_edge("describe", END)
    return graph.compile()


def default_deps(before_call=None) -> FindDeps:
    from sources.registry import build_providers

    providers = build_providers(before_call)
    return FindDeps(shopping=providers.shopping, supplement=providers.supplement, limits=FindLimits.from_config())


#: New searches allowed when refining an earlier find (its listings are reused).
REFINE_MAX_QUERIES = 2


def refine_description(original: str, followups: list[str]) -> str:
    lines = [" ".join(original.split())] + [f"Follow-up: {' '.join(f.split())}" for f in followups]
    return "\n".join(lines)


def run_find(
    description: str,
    *,
    deps: FindDeps | None = None,
    constraints: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
    followup: str | None = None,
) -> dict[str, Any]:
    """Run the Find graph and return recommendations, evidence and a stage trace.

    With ``previous`` (an earlier result) and ``followup``, the find is refined: the
    earlier listings and queries are kept, the agents re-read the need with the
    follow-up, and at most ``REFINE_MAX_QUERIES`` new searches are made.
    """
    description = description.strip()
    if not description:
        raise ValueError("Please describe what you are looking for.")
    deps = deps or default_deps()
    if previous is not None:
        deps = replace(deps, limits=replace(deps.limits, max_queries=min(deps.limits.max_queries, REFINE_MAX_QUERIES)))
    started = deps.clock()
    seed = {e["id"]: dict(e) for e in (previous or {}).get("evidence") or [] if e.get("id")}
    state = build_graph(deps).invoke(
        {
            "description": description,
            "followup": " ".join(str(followup or "").split()) or None,
            "constraints": dict(constraints or {}),
            "started": started,
            "rewrite_rounds": 0,
            "searches": 0,
            "supplement_searches": 1 if previous is not None else 0,
            "evidence": seed,
            "stages": {},
            "notes": [],
            "all_queries": list((previous or {}).get("queries") or []),
        }
    )
    recs = state.get("recommendations") or []
    notes = [n for n in state.get("notes") or [] if n]
    if not recs:
        notes.append("No suitable product with evidence was found.")
    return {
        "status": STATUS_READY if recs else STATUS_PARTIAL,
        "description": description,
        "need": state.get("need") or {},
        "queries": state.get("all_queries") or [],
        "recommendations": recs,
        "evidence": list((state.get("evidence") or {}).values()),
        "stages": state.get("stages") or {},
        "notes": notes,
        "usage": {
            "searches": state.get("searches", 0),
            "supplement_searches": state.get("supplement_searches", 0),
            "rewrite_rounds": state.get("rewrite_rounds", 0),
            "elapsed_seconds": round(max(0.0, deps.clock() - started), 3),
        },
    }
