# Evaluation

The proposal's three accuracy dimensions are measured by `python -m evaluation` (`evaluation/runner.py`, cases in `evaluation/datasets.py`).

| Track | Proposal metric | What runs | Headline numbers |
| --- | --- | --- | --- |
| `offer_price_parsing` | Price extraction | Google Shopping price strings → amount + currency (deterministic), 22 cases, 17 merchants | exact accuracy, **false-price rate**, trusted vs unverified accuracy |
| `page_price_extraction` | Price extraction | product-page text → price-extractor agent, 12 cases incl. no-price pages and an injection attempt | price/stock accuracy, **false-price rate** |
| `instruction_parsing` | Instruction parsing | task-parser agent, 16 Chinese/English instructions incl. invented URLs, bad currencies, out-of-range frequency | full-match and per-field accuracy |
| `notification_trigger` | Notification trigger | 10 check sequences (19 steps) through the real scheduler: drops, untrusted/second-hand/foreign-currency offers, stock changes, failures, SMTP retry, many sellers | send precision/recall, **unexpected-send rate** |

## Modes

- **Offline (default):** model answers are replayed from the dataset through the real agents, `llm.call_json` and validators. This measures validation, normalisation and decision logic, not model quality. Current result: every case passes; false-price and unexpected-send rates are 0.
- **Live (`--live`):** page-extraction and instruction cases are sent to the configured model. This is the real model-accuracy measurement and costs model calls; record the date, model id and results when you run it.

## Not yet measured

- Real SerpApi coverage and price agreement against manually checked NZ retailer pages (proposal: 20+ products, 3+ retailers). Plan: pick 20 products, record SerpApi offers and the retailer page price on the same day, add them as `observed` cases.
- One-week continuous monitoring with real notifications (proposal: notification accuracy over a week).
- The instruction set is smaller than the proposal's 50+ commands; extend `INSTRUCTION_CASES` with colloquial and multi-condition requests.
