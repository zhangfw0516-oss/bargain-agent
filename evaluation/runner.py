"""Deterministic, fully offline runner for the Stage E evaluation.

Usage (from the repository root)::

    ./venv/Scripts/python.exe -m evaluation            # human summary to stdout
    ./venv/Scripts/python.exe -m evaluation --json out.json

The runner only calls the project's local deterministic code paths with injected
fakes. It never reads ``.env``, never constructs a config value, and never performs
a real network / LLM / Brave / merchant / SMTP call. See ``evaluation/datasets.py``
for the case sets and their provenance tags.

Every reported metric carries an explicit denominator. Numbers describe the *offline
logic*, not real DeepSeek parsing or real-site accuracy — those are reported
separately as already-recorded evidence plus gaps in ``docs/EVALUATION.md``.
"""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

# Keep the web-provider path explicitly unconfigured for this process, so no
# accidental default discovery could ever reach a real search. This is set before
# any business module is imported and does not read or modify .env.
os.environ.setdefault("BRAVE_SEARCH_API_KEY", "")

import discovery  # noqa: E402
import schemas  # noqa: E402
import scheduler  # noqa: E402
import scraper  # noqa: E402
import storage  # noqa: E402
from evaluation import datasets  # noqa: E402

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _provenance_counts(cases: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        key = case.get("provenance", "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _case_record(case_id, provenance, expected, actual, passed, note=""):
    return {
        "id": case_id,
        "provenance": provenance,
        "expected": expected,
        "actual": actual,
        "passed": bool(passed),
        "note": note,
    }


def _track(name, description, cases, metrics, extra=None):
    failures = [c for c in cases if not c["passed"]]
    track = {
        "name": name,
        "description": description,
        "n_cases": len(cases),
        "provenance": _provenance_counts(cases),
        "metrics": metrics,
        "cases": cases,
        "failures": failures,
    }
    if extra:
        track.update(extra)
    return track


# ---------------------------------------------------------------------------
# Track 1 — local instruction/schema validation
# ---------------------------------------------------------------------------


def _schema_passed(expected: dict, actual: dict) -> bool:
    if expected["ok"] != actual.get("ok"):
        return False
    if not expected["ok"]:
        return True
    if "status" in expected and expected["status"] != actual.get("status"):
        return False
    if "budget" in expected and expected.get("budget") is not None:
        if expected["budget"] != actual.get("budget"):
            return False
    if "missing_fields" in expected:
        if sorted(expected["missing_fields"]) != sorted(actual.get("missing_fields") or []):
            return False
    return True


def run_schema_track() -> dict:
    cases: list[dict] = []
    for spec in datasets.SCHEMA_CASES:
        validator = schemas.validate_task if spec["validator"] == "task" else schemas.validate_search_request
        try:
            result = validator(dict(spec["data"]), spec["instruction"])
        except ValueError as exc:
            actual = {"ok": False, "error": str(exc)}
        else:
            actual = {
                "ok": True,
                "status": result.get("status"),
                "budget": result.get("budget", result.get("target_price")),
                "missing_fields": sorted(result.get("missing_fields") or []),
            }
        cases.append(
            _case_record(spec["id"], spec["provenance"], spec["expected"], actual,
                         _schema_passed(spec["expected"], actual), spec["rationale"])
        )

    expected_accept = [s for s in datasets.SCHEMA_CASES if s["expected"]["ok"]]
    expected_reject = [s for s in datasets.SCHEMA_CASES if not s["expected"]["ok"]]
    by_id = {c["id"]: c for c in cases}
    false_accept = sum(
        1 for s in expected_reject if by_id[s["id"]]["actual"].get("ok") is True
    )
    false_reject = sum(
        1 for s in expected_accept if by_id[s["id"]]["actual"].get("ok") is False
    )
    correct = sum(1 for c in cases if c["passed"])
    metrics = {
        "n_cases": len(cases),
        "correct": correct,
        "accuracy": _ratio(correct, len(cases)),
        "n_expected_accept": len(expected_accept),
        "n_expected_reject": len(expected_reject),
        "false_accept_dangerous": false_accept,
        "false_reject": false_reject,
        "accept_recall": _ratio(len(expected_accept) - false_reject, len(expected_accept)),
        "reject_recall": _ratio(len(expected_reject) - false_accept, len(expected_reject)),
    }
    return _track(
        "instruction_schema",
        "Local validation of parsed instruction dicts (the guard, not the LLM).",
        cases, metrics,
    )


def run_demo_parser_track() -> dict:
    import demo_data

    cases: list[dict] = []
    for spec in datasets.DEMO_PARSER_CASES:
        try:
            parsed = demo_data.parse_demo_request(spec["instruction"])
            validated = schemas.validate_search_request(parsed, spec["instruction"])
        except ValueError as exc:
            actual = {"error": str(exc)}
        else:
            actual = {
                "status": validated.get("status"),
                "product_name": validated.get("product_name"),
                "colour": validated.get("colour"),
                "budget": validated.get("budget"),
                "currency": validated.get("currency"),
                "frequency_minutes": validated.get("frequency_minutes"),
                "missing_fields": sorted(validated.get("missing_fields") or []),
            }
        expected = spec["expected"]
        passed = all(actual.get(k) == v for k, v in expected.items())
        cases.append(
            _case_record(spec["id"], spec["provenance"], expected, actual, passed,
                         "Offline demo lexical parser (NOT the real LLM).")
        )
    correct = sum(1 for c in cases if c["passed"])
    metrics = {
        "n_cases": len(cases),
        "correct": correct,
        "accuracy": _ratio(correct, len(cases)),
        "note": "Deterministic demo parser; must not be reported as LLM accuracy.",
    }
    return _track(
        "demo_parser_pipeline",
        "Offline demo lexical parser -> schema validator. NOT real LLM parsing.",
        cases, metrics,
    )


# ---------------------------------------------------------------------------
# Track 2 — product identity
# ---------------------------------------------------------------------------

_LABELS = (discovery.MATCH_SAME, discovery.MATCH_DIFFERENT, discovery.MATCH_NEEDS_CONFIRMATION)


def run_identity_track() -> dict:
    cases: list[dict] = []
    for spec in datasets.IDENTITY_CASES:
        level = spec["level"]
        if level == "candidate":
            actual, reasons = discovery.match_candidate(dict(spec["request"]), spec["candidate"])
        elif level == "detail":
            actual, reasons = discovery._detail_identity(
                dict(spec["request"]), spec["entry"], spec["quote"]
            )
        else:
            actual = scheduler.quote_identity_verdict(spec["identity"], spec["quote"])
            reasons = []
        passed = actual == spec["expected"]
        cases.append(
            _case_record(
                spec["id"], spec["provenance"],
                {"level": level, "verdict": spec["expected"]},
                {"verdict": actual, "reasons": reasons},
                passed, spec["rationale"],
            )
        )

    expected_same = [s for s in datasets.IDENTITY_CASES if s["expected"] == discovery.MATCH_SAME]
    by_id = {c["id"]: c for c in cases}
    same_tp = sum(1 for s in expected_same if by_id[s["id"]]["actual"]["verdict"] == discovery.MATCH_SAME)
    same_fp = sum(
        1 for s in datasets.IDENTITY_CASES
        if s["expected"] != discovery.MATCH_SAME
        and by_id[s["id"]]["actual"]["verdict"] == discovery.MATCH_SAME
    )
    same_fn = len(expected_same) - same_tp
    wrong_item_false_accepts = sum(
        1 for s in datasets.IDENTITY_CASES
        if s["expected"] == discovery.MATCH_DIFFERENT
        and by_id[s["id"]]["actual"]["verdict"] == discovery.MATCH_SAME
    )
    unverifiable_false_accepts = sum(
        1 for s in datasets.IDENTITY_CASES
        if s["expected"] == discovery.MATCH_NEEDS_CONFIRMATION
        and by_id[s["id"]]["actual"]["verdict"] == discovery.MATCH_SAME
    )
    correct = sum(1 for c in cases if c["passed"])
    total = len(cases)
    metrics = {
        "n_cases": total,
        "correct": correct,
        "accuracy": _ratio(correct, total),
        "n_expected_same": len(expected_same),
        "same_true_positives": same_tp,
        "same_false_positives": same_fp,
        "same_false_negatives": same_fn,
        "same_precision": _ratio(same_tp, same_tp + same_fp),
        "same_recall": _ratio(same_tp, same_tp + same_fn),
        "wrong_item_false_accepts": wrong_item_false_accepts,
        "unverifiable_false_accepts": unverifiable_false_accepts,
        "checked_by_level": {
            level: sum(1 for s in datasets.IDENTITY_CASES if s["level"] == level)
            for level in ("candidate", "detail", "monitor")
        },
    }
    return _track(
        "identity",
        "same / different / needs_confirmation across search cards, detail pages and monitoring.",
        cases, metrics,
    )


# ---------------------------------------------------------------------------
# Track 3 — static page quote extraction (amount + currency + stock together)
# ---------------------------------------------------------------------------


def run_quote_track() -> dict:
    cases: list[dict] = []
    extraction_total = 0
    refusal_total = 0
    triple_exact = 0
    price_ok = currency_ok = stock_ok = 0
    refused_correct = 0

    for spec in datasets.QUOTE_CASES:
        expected = spec["expected"]
        is_refusal = bool(expected.get("refuse"))
        refused = False
        actual: dict[str, Any]
        try:
            quote = scraper._parse_page(spec["url"], spec["html"], spec["url"])
        except scraper.ScraperError as exc:
            refused = True
            actual = {"refused": True, "error": str(exc)}
        else:
            actual = {
                "refused": False,
                "price": quote.get("price"),
                "currency": quote.get("currency"),
                "in_stock": quote.get("in_stock"),
            }

        if is_refusal:
            refusal_total += 1
            passed = refused
            refused_correct += 1 if refused else 0
        else:
            extraction_total += 1
            p_ok = actual.get("price") == expected["price"]
            c_ok = actual.get("currency") == expected["currency"]
            s_ok = actual.get("in_stock") == expected["in_stock"]
            price_ok += 1 if p_ok else 0
            currency_ok += 1 if c_ok else 0
            stock_ok += 1 if s_ok else 0
            passed = (not refused) and p_ok and c_ok and s_ok
            triple_exact += 1 if passed else 0

        cases.append(
            _case_record(
                spec["id"], spec["provenance"], expected, actual, passed,
                spec.get("source", "in-memory HTML"),
            )
        )

    total = len(cases)
    metrics = {
        "n_cases": total,
        "n_extraction_cases": extraction_total,
        "n_refusal_cases": refusal_total,
        "triple_exact": triple_exact,
        "triple_exact_rate": _ratio(triple_exact, extraction_total),
        "price_field_ok": price_ok,
        "currency_field_ok": currency_ok,
        "stock_field_ok": stock_ok,
        "refused_correct": refused_correct,
        "refusal_rate": _ratio(refused_correct, refusal_total),
        "overall_accuracy": _ratio(triple_exact + refused_correct, total),
    }
    return _track(
        "quote_extraction",
        "Static JSON-LD/OpenGraph quote parsing; amount, currency and stock verified together.",
        cases, metrics,
    )


# ---------------------------------------------------------------------------
# Track 4 — two-source deal notification: dedup + failure isolation
# ---------------------------------------------------------------------------

_A_URL = "https://shop-a.example/product/air"
_B_URL = "https://shop-b.example/product/air"
_IDENTITY = {
    "product_name": "Fitbit Air",
    "colour": "black",
    "capacity": None,
    "confirmed_title": "Google Fitbit Air Fitness Tracker - Obsidian",
}


def _notif_quote(url: str, price: str, *, in_stock: bool | None = True,
                 product_name: str = "Google Fitbit Air (Obsidian)") -> dict:
    return {
        "product_name": product_name,
        "url": url,
        "retailer": "shop",
        "price": price,
        "currency": "NZD",
        "in_stock": in_stock,
        "fetched_at": "2026-09-25T00:00:00+00:00",
        "shipping_cost": None,
    }


class _Recorder:
    """Fake send function; records every call and its success flag."""

    def __init__(self, behaviour: Callable[[int], dict] | None = None):
        self.calls: list[dict] = []
        self._behaviour = behaviour

    def __call__(self, email: str, task: dict, quote: dict) -> dict:
        result = {"success": True, "attempts": 1}
        if self._behaviour is not None:
            result = self._behaviour(len(self.calls) + 1)
        self.calls.append({
            "email": email, "task_id": task.get("id"),
            "price": quote.get("price"), "success": bool(result.get("success")),
        })
        return result

    @property
    def count(self) -> int:
        return len(self.calls)

    @property
    def successes(self) -> int:
        return sum(1 for c in self.calls if c["success"])


def _fetcher(mapping: dict[str, Any]) -> Callable[[str], dict]:
    def fetch(url: str) -> dict:
        value = mapping.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RuntimeError(f"No configured quote for {url}")
        return dict(value)
    return fetch


def _make_two_source_task(db_path: Path) -> dict:
    task = {
        "product_name": "Google Fitbit Air (Obsidian)",
        "url": _A_URL,
        "target_price": "250.00",
        "currency": "NZD",
        "comparison": "lte",
        "frequency_minutes": 60,
        "notify_email": "eval@example.com",
        "status": "active",
        "identity": dict(_IDENTITY),
    }
    task_id = storage.create_task_with_sources(
        task,
        [{"url": _A_URL, "retailer": "Shop A"}, {"url": _B_URL, "retailer": "Shop B"}],
        db_path=db_path,
    )
    return storage.get_task(task_id, db_path=db_path)


def _run_scenario(scenario_id: str, db_path: Path) -> dict:
    """Execute one scenario and return the observed values keyed for comparison."""

    def check(task, mapping, recorder):
        return scheduler.check_task(
            task, fetcher=_fetcher(mapping), send_fn=recorder,
            max_retries=3, db_path=db_path,
        )

    if scenario_id == "notif_both_deal_one_email":
        task = _make_two_source_task(db_path)
        rec = _Recorder()
        r1 = check(task, {_A_URL: _notif_quote(_A_URL, "239.00"),
                          _B_URL: _notif_quote(_B_URL, "245.00")}, rec)
        r2 = check(task, {_A_URL: _notif_quote(_A_URL, "239.00"),
                          _B_URL: _notif_quote(_B_URL, "245.00")}, rec)
        return {"send_calls": rec.count, "check1_is_deal": r1["is_deal"],
                "check2_is_deal": r2["is_deal"], "check2_notified": r2["notified"],
                "sources_failed": r1["sources_failed"]}

    if scenario_id == "notif_equal_price_one_email":
        task = _make_two_source_task(db_path)
        rec = _Recorder()
        r1 = check(task, {_A_URL: _notif_quote(_A_URL, "239.00"),
                          _B_URL: _notif_quote(_B_URL, "239.00")}, rec)
        return {"send_calls": rec.count, "is_deal": r1["is_deal"]}

    if scenario_id == "notif_source_failure_isolated":
        task = _make_two_source_task(db_path)
        rec = _Recorder()
        r1 = check(task, {_A_URL: RuntimeError("Shop A unreachable"),
                          _B_URL: _notif_quote(_B_URL, "239.00")}, rec)
        return {"send_calls": rec.count, "is_deal": r1["is_deal"],
                "sources_failed": r1["sources_failed"], "sources_checked": r1["sources_checked"]}

    if scenario_id == "notif_failure_keeps_episode_open":
        task = _make_two_source_task(db_path)
        rec = _Recorder()
        check(task, {_A_URL: _notif_quote(_A_URL, "239.00"),
                     _B_URL: _notif_quote(_B_URL, "245.00")}, rec)
        r2 = check(task, {_A_URL: RuntimeError("Shop A unreachable"),
                          _B_URL: _notif_quote(_B_URL, "260.00")}, rec)
        return {"send_calls": rec.count, "event_reset": r2.get("event_reset", False),
                "event_reset_skipped": r2.get("event_reset_skipped", False)}

    if scenario_id == "notif_out_of_stock_not_chosen":
        task = _make_two_source_task(db_path)
        rec = _Recorder()
        r1 = check(task, {_A_URL: _notif_quote(_A_URL, "239.00", in_stock=False),
                          _B_URL: _notif_quote(_B_URL, "245.00", in_stock=True)}, rec)
        return {"send_calls": rec.count, "chosen_price": r1.get("chosen_price")}

    if scenario_id == "notif_all_above_target_resets":
        task = _make_two_source_task(db_path)
        rec = _Recorder()
        check(task, {_A_URL: _notif_quote(_A_URL, "239.00"),
                     _B_URL: _notif_quote(_B_URL, "245.00")}, rec)
        r2 = check(task, {_A_URL: _notif_quote(_A_URL, "260.00"),
                          _B_URL: _notif_quote(_B_URL, "300.00")}, rec)
        return {"send_calls": rec.count, "event_reset": r2.get("event_reset", False),
                "second_is_deal": r2["is_deal"]}

    if scenario_id == "notif_accessory_page_ignored":
        task = _make_two_source_task(db_path)
        rec = _Recorder()
        r1 = check(task, {_A_URL: _notif_quote(_A_URL, "19.00",
                                               product_name="Google Fitbit Air Active Band"),
                          _B_URL: _notif_quote(_B_URL, "239.00")}, rec)
        return {"send_calls": rec.count, "chosen_price": r1.get("chosen_price")}

    if scenario_id == "notif_send_retry_then_success":
        task = _make_two_source_task(db_path)
        rec = _Recorder(lambda n: {"success": False, "retryable": True, "error": "smtp down"}
                        if n == 1 else {"success": True, "attempts": 1})
        r1 = check(task, {_A_URL: _notif_quote(_A_URL, "239.00"),
                          _B_URL: _notif_quote(_B_URL, "245.00")}, rec)
        r2 = check(task, {_A_URL: _notif_quote(_A_URL, "239.00"),
                          _B_URL: _notif_quote(_B_URL, "245.00")}, rec)
        return {"send_calls": rec.count, "first_notified": r1["notified"],
                "second_notified": r2["notified"], "success_sends": rec.successes}

    if scenario_id == "notif_permanent_send_failure":
        task = _make_two_source_task(db_path)
        rec = _Recorder(lambda n: {"success": False, "retryable": False, "error": "bad address"})
        r1 = check(task, {_A_URL: _notif_quote(_A_URL, "239.00"),
                          _B_URL: _notif_quote(_B_URL, "245.00")}, rec)
        return {"send_calls": rec.count, "notified": r1["notified"],
                "reason": r1.get("notification_reason")}

    raise ValueError(f"Unknown scenario: {scenario_id}")


@contextmanager
def _quiet_scheduler_logs():
    """The injected source failure is expected; do not spam the report with it."""
    logger = logging.getLogger("scheduler")
    previous = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(previous)


def run_notification_track() -> dict:
    cases: list[dict] = []
    for spec in datasets.NOTIFICATION_SCENARIOS:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = Path(tmp.name)
        try:
            storage.init_db(db_path)
            with _quiet_scheduler_logs():
                observed = _run_scenario(spec["id"], db_path)
        finally:
            db_path.unlink(missing_ok=True)
        expected = spec["expected"]
        passed = all(observed.get(k) == v for k, v in expected.items())
        cases.append(
            _case_record(spec["id"], spec["provenance"], expected, observed, passed,
                         spec["description"])
        )

    correct = sum(1 for c in cases if c["passed"])
    dedup_specs = [s for s in datasets.NOTIFICATION_SCENARIOS if "send_calls" in s["expected"]]
    # Failure isolation is an explicit, independently-tagged property of a scenario
    # (it must inject a source or send failure), never inferred from whatever keys
    # happen to sit in ``expected``.
    isolation_specs = [
        s for s in datasets.NOTIFICATION_SCENARIOS
        if "failure_isolation" in s.get("tags", [])
    ]
    by_id = {c["id"]: c for c in cases}
    dedup_ok = sum(1 for s in dedup_specs if by_id[s["id"]]["passed"])
    isolation_ok = sum(1 for s in isolation_specs if by_id[s["id"]]["passed"])
    total_emails = sum(c["actual"].get("send_calls", 0) for c in cases)
    metrics = {
        "n_scenarios": len(cases),
        "correct": correct,
        "accuracy": _ratio(correct, len(cases)),
        "n_dedup_scenarios": len(dedup_specs),
        "dedup_correct": dedup_ok,
        "n_isolation_scenarios": len(isolation_specs),
        "isolation_correct": isolation_ok,
        "total_fake_emails_sent": total_emails,
    }
    return _track(
        "notification_dedup",
        "Two-source aggregated checks: one email per deal window; a failing source is isolated.",
        cases, metrics,
    )


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def run_all() -> dict:
    tracks = [
        run_schema_track(),
        run_demo_parser_track(),
        run_identity_track(),
        run_quote_track(),
        run_notification_track(),
    ]
    return {
        "schema_version": "1",
        "generated_by": "evaluation.runner",
        "offline": True,
        "note": (
            "All results are from local deterministic code with injected fakes. "
            "No real LLM / Brave / merchant / SMTP call was made and .env was not read. "
            "These are not real-service accuracy figures."
        ),
        "tracks": {t["name"]: t for t in tracks},
    }


def format_summary(report: dict) -> str:
    lines = ["Bargain Agent offline evaluation", "=" * 34]
    for name, track in report["tracks"].items():
        m = track["metrics"]
        lines.append("")
        lines.append(f"[{name}] {track['n_cases']} cases, {len(track['failures'])} failed")
        for key, value in m.items():
            if key == "note":
                continue
            lines.append(f"    {key}: {value}")
        for failure in track["failures"]:
            lines.append(f"    FAIL {failure['id']}: expected={failure['expected']} actual={failure['actual']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="evaluation", description="Run the offline Stage E evaluation.")
    parser.add_argument("--json", metavar="PATH", help="Also write the full report as JSON to PATH.")
    args = parser.parse_args(argv)

    report = run_all()
    print(format_summary(report))
    total_cases = sum(t["n_cases"] for t in report["tracks"].values())
    total_failures = sum(len(t["failures"]) for t in report["tracks"].values())
    print("")
    print(f"TOTAL: {total_cases} cases, {total_failures} failed")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote {args.json}")
    return 1 if total_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
