"""Evaluation runner for the three proposal metrics.

Offline by default: model answers are replayed from ``datasets`` through the real
agents and validators (``llm.call_json`` with a fake client), so the numbers
measure the local validation, normalisation and decision logic. ``--live`` sends
the page-extraction and instruction cases to the configured real model instead,
which is the real-accuracy measurement; it costs model calls.

Run with ``python -m evaluation [--live] [--json]``.
"""

from __future__ import annotations

import argparse
import json
import logging
import tempfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import scheduler
import storage
from agents import LLMOptions, price_extractor, task_parser
from evaluation import datasets
from pricing import merchant_trust
from sources.base import parse_price


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)


def _track(name: str, description: str, cases: list[dict], metrics: dict, provenance: Counter | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "n_cases": len(cases),
        "metrics": metrics,
        "cases": cases,
        "failures": [c["id"] for c in cases if not c["passed"]],
        "provenance": dict(provenance or {}),
    }


class _ReplayClient:
    """A chat client that returns one fixed JSON answer (offline replay)."""

    def __init__(self, answer: Any):
        text = json.dumps(answer, ensure_ascii=False)
        choice = SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=text))
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_: SimpleNamespace(choices=[choice])))


def _opts(case: dict, live: bool) -> LLMOptions:
    return LLMOptions() if live else LLMOptions(client=_ReplayClient(case["replay"]), model="replay")


# --------------------------------------------------------------------------- 1a


def run_offer_price_track() -> dict:
    cases, groups = [], {"trusted": [0, 0], "unverified": [0, 0]}
    false_prices = no_price_gold = 0
    for case in datasets.OFFER_PRICE_CASES:
        actual = parse_price(case["raw"], case["extracted"], default_dollar="NZD")
        gold = tuple(case["gold"])
        passed = actual == gold
        group = "trusted" if merchant_trust(case["merchant"]) else "unverified"
        groups[group][0] += passed
        groups[group][1] += 1
        if gold[0] is None:
            no_price_gold += 1
            false_prices += actual[0] is not None
        cases.append({"id": case["id"], "merchant": case["merchant"], "trusted": group == "trusted",
                      "expected": list(gold), "actual": list(actual), "passed": passed})
    passed = sum(c["passed"] for c in cases)
    metrics = {
        "exact_accuracy": _ratio(passed, len(cases)),
        "amount_accuracy": _ratio(sum(c["actual"][0] == c["expected"][0] for c in cases), len(cases)),
        "currency_accuracy": _ratio(sum(c["actual"][1] == c["expected"][1] for c in cases), len(cases)),
        "false_price_rate": _ratio(false_prices, no_price_gold),
        "trusted_accuracy": _ratio(*groups["trusted"]),
        "unverified_accuracy": _ratio(*groups["unverified"]),
        "merchants": len({c["merchant"] for c in cases}),
    }
    provenance = Counter(c["provenance"] for c in datasets.OFFER_PRICE_CASES)
    return _track("offer_price_parsing", "Google Shopping price strings -> amount + currency (deterministic).",
                  cases, metrics, provenance)


# --------------------------------------------------------------------------- 1b


def run_page_price_track(live: bool = False) -> dict:
    cases = []
    false_prices = no_price_gold = 0
    for case in datasets.PAGE_PRICE_CASES:
        try:
            out = price_extractor.run(case["page_text"], url=case.get("url"), opts=_opts(case, live))
            actual = {"price": out["price"], "currency": out["currency"], "in_stock": out["in_stock"]}
        except price_extractor.PriceExtractionError:
            actual = None
        gold = case["gold"]
        if gold is None:
            no_price_gold += 1
            false_prices += actual is not None
        cases.append({"id": case["id"], "expected": gold, "actual": actual, "passed": actual == gold})
    priced = [c for c in cases if c["expected"] is not None]
    metrics = {
        "exact_accuracy": _ratio(sum(c["passed"] for c in cases), len(cases)),
        "price_accuracy": _ratio(sum(bool(c["actual"]) and c["actual"]["price"] == c["expected"]["price"] for c in priced), len(priced)),
        "stock_accuracy": _ratio(sum(bool(c["actual"]) and c["actual"]["in_stock"] == c["expected"]["in_stock"] for c in priced), len(priced)),
        "false_price_rate": _ratio(false_prices, no_price_gold),
        "mode": "live" if live else "replay",
    }
    provenance = Counter(c["provenance"] for c in datasets.PAGE_PRICE_CASES)
    return _track("page_price_extraction", "Product-page text -> price via the price-extractor agent.", cases, metrics, provenance)


# --------------------------------------------------------------------------- 2


def run_instruction_track(live: bool = False) -> dict:
    cases = []
    field_hits = field_total = 0
    for case in datasets.INSTRUCTION_CASES:
        gold = case["gold"]
        try:
            out = task_parser.run(case["instruction"], opts=_opts(case, live))
        except task_parser.TaskParseError:
            out = {"status": "rejected"}
        if gold.get("status") == "no_task" and out.get("status") != "ready":
            # Safe outcomes that create no task: rejected by validation, a
            # clarification question, or declined as unsupported.
            out = {**out, "status": "no_task"}
        fields = {k: out.get(k) for k in gold}
        hits = sum(fields[k] == v for k, v in gold.items())
        field_hits += hits
        field_total += len(gold)
        cases.append({"id": case["id"], "expected": gold, "actual": fields, "passed": hits == len(gold)})
    metrics = {
        "full_match_accuracy": _ratio(sum(c["passed"] for c in cases), len(cases)),
        "field_accuracy": _ratio(field_hits, field_total),
        "mode": "live" if live else "replay",
    }
    return _track("instruction_parsing", "Natural-language monitoring instruction -> task fields (task-parser agent).",
                  cases, metrics, Counter({datasets.PROVENANCE_SYNTHETIC: len(cases)}))


# --------------------------------------------------------------------------- 3


@contextmanager
def _quiet_logs():
    log = logging.getLogger("scheduler")
    level = log.level
    log.setLevel(logging.WARNING)
    try:
        yield
    finally:
        log.setLevel(level)


def _run_scenario(scenario: dict, db_path: Path) -> list[dict]:
    storage.init_db(db_path)
    task_id = storage.create_task_with_sources(
        {"product_name": "Fitbit Charge 6", "url": "https://www.google.co.nz/shopping/product/1", "target_price": "230.00",
         "currency": "NZD", "comparison": "lte", "frequency_minutes": 1440, "notify_email": "eval@example.com"},
        [{"url": "https://www.google.co.nz/shopping/product/1", "source_kind": storage.SOURCE_SERPAPI, "product_id": "1"}],
        db_path=db_path,
    )
    smtp = list(scenario.get("smtp") or [])
    sent: list[int] = []
    results = []
    for index, (observed, expected) in enumerate(scenario["steps"]):
        def observer(_source, _task, observed=observed):
            if isinstance(observed, str):
                raise scheduler.pricing.ObservationError(observed)
            return [dict(o) for o in observed]

        def send(_to, _task, _quote, index=index):
            result = smtp.pop(0) if smtp else {"success": True}
            if result.get("success"):
                sent.append(index)
            return result

        task = storage.get_task(task_id, db_path=db_path)
        scheduler.check_task(task, observer=observer, verifier=None, send_fn=send, max_retries=3, db_path=db_path)
        results.append({"step": index, "expected_send": expected, "sent": index in sent})
    return results


def run_notification_track() -> dict:
    cases = []
    tp = fp = fn = steps = 0
    with tempfile.TemporaryDirectory() as tmp, _quiet_logs():
        for scenario in datasets.NOTIFICATION_SCENARIOS:
            results = _run_scenario(scenario, Path(tmp) / f"{scenario['id']}.db")
            for r in results:
                steps += 1
                tp += r["expected_send"] and r["sent"]
                fp += r["sent"] and not r["expected_send"]
                fn += r["expected_send"] and not r["sent"]
            expected = [r["expected_send"] for r in results]
            actual = [r["sent"] for r in results]
            cases.append({"id": scenario["id"], "description": scenario["description"],
                          "expected": expected, "actual": actual, "passed": expected == actual})
    metrics = {
        "scenario_accuracy": _ratio(sum(c["passed"] for c in cases), len(cases)),
        "send_precision": _ratio(tp, tp + fp),
        "send_recall": _ratio(tp, tp + fn),
        "unexpected_send_rate": _ratio(fp, steps),
        "steps": steps,
    }
    return _track("notification_trigger", "Check sequences through the real scheduler with fake observer/notifier.",
                  cases, metrics, Counter({datasets.PROVENANCE_SYNTHETIC: len(cases)}))


# --------------------------------------------------------------------------- report


def run_all(live: bool = False) -> dict:
    tracks = [run_offer_price_track(), run_page_price_track(live), run_instruction_track(live), run_notification_track()]
    return {"mode": "live" if live else "offline", "tracks": {t["name"]: t for t in tracks}}


def format_summary(report: dict) -> str:
    lines = [f"Bargain Agent evaluation ({report['mode']})"]
    for track in report["tracks"].values():
        lines.append(f"\n{track['name']} — {track['n_cases']} cases: {track['description']}")
        for key, value in track["metrics"].items():
            lines.append(f"  {key}: {value}")
        if track["failures"]:
            lines.append(f"  failures: {', '.join(track['failures'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation", description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="Use the configured real model (costs model calls).")
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON.")
    args = parser.parse_args(argv)
    report = run_all(live=args.live)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_summary(report))
    return 0
