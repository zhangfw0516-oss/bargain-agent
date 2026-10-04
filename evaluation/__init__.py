"""Deterministic, fully offline evaluation assets for the Bargain Agent (Stage E).

Nothing under this package performs a real network, LLM, Brave, merchant or SMTP
call, and nothing here reads ``.env`` or any credential. The runner only exercises
the project's *local* deterministic code paths (schema validation, identity
matching, static-page quote parsing, aggregated two-source checks) with injected
fakes, so the numbers it produces describe the offline logic — not real-service
accuracy. Real-service evidence is reported separately, as evidence already on
record, in ``docs/EVALUATION.md``.

See ``evaluation/runner.py`` for the runner and ``evaluation/datasets.py`` for the
fixed case sets.
"""

__all__ = ["datasets", "runner"]
