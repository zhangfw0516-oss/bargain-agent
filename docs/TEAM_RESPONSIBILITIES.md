# Team Responsibilities

Who owns which part of the final system, and which files belong to it. Roles follow [PROPOSAL.md](../PROPOSAL.md) §8 and the team table in the [README](../README.md), adjusted to the final architecture: retailer scraping became search APIs, and eight AI agents were added. Each person presents the part they own in the final demo (see `presentation/`).

| Member | Role | Owns | Presents (demo order) |
| :--- | :--- | :--- | :--- |
| Fengwei Zhang | Project Lead | Architecture, integration, configuration, packaging and delivery, documentation, evaluation summary, presentation | 1 · Purpose, design, evaluation results, engineering |
| Tianshuo Gao | Interface Developer | Web UI and API, CLI task management, email notifications | 2 · Live Find and refine |
| Xiao Zhang | Agent Developer & Test | Find agents, same-product matching | 3 · Agents and same-product matching |
| Gang Zhao | Data Engineer | Search providers, price observation, price database, demo data, real price collection | 4 · Data sources and price history |
| Wenhan Zhang | Agent Developer & Test | Monitoring scheduler, deal decisions, instruction parsing, price extraction, output validation | 5 · Alerts, tests, limits and summary |

## Fengwei Zhang · Project Lead

PROPOSAL: progress oversight and milestones; overall architecture and System Context Diagram; supervisor liaison; full system integration and final documentation.

- **Architecture and integration:** `pipeline.py` (LangGraph Find pipeline), `llm.py` (the single validated LLM entry point), `config.py`, `config_setup.py`, `main.py`
- **Packaging and delivery:** `build_package.py`, `launcher.py`, `start.bat`, `check-due.bat`, `start-demo.bat`, `record-voice.bat`, `requirements.txt`, `.env.example`, `.gitignore`, `.gitattributes`
- **Documentation:** `README.md`, `PROPOSAL.md`, `PROJECT_HANDOVER.md`, `docs/SYSTEM_CONTEXT.md`, `docs/INTERFACES.md`, `docs/COURSE_DELIVERY_CHECKLIST.md`, `docs/EVALUATION.md` (consolidated results), `docs/TEAM_RESPONSIBILITIES.md`, `project_progress/DECISIONS.md`, `project_progress/KNOWN_ISSUES.md`
- **Presentation and reports:** `presentation/` (scripts, narrated video, voice recorder), `report_work/`, `deliverables/`, `archive/`
- **Test infrastructure and integration tests:** `tests/__init__.py`, `tests/fakes.py`, `tests/test_integration.py`, `tests/test_config.py`, `tests/test_llm.py`, `tests/test_zz_offline_network_guard.py`

## Gang Zhao · Data Engineer

PROPOSAL: crawler development, anti-crawl handling, price database; assists with data accuracy test cases.

- **Search providers and page readers:** `sources/` (`__init__.py`, `base.py`, `serper.py`, `serpapi.py`, `firecrawl.py`, `scrapedo.py`, `tavily.py`, `registry.py`)
- **Price observation and database:** `pricing.py` (observing sources, quota guard, merchant trust, shipping, 30-day price insight), `storage.py` (SQLite schema and migrations)
- **Demo data:** `demo/__init__.py`, `demo/seed_demo.py`, `demo/listings.json`
- **Real price accuracy collection:** `evaluation/collect_live.py`, `evaluation/live_results/`
- **Tests:** `tests/test_sources.py`, `tests/test_storage.py`, `tests/fixtures/`

## Tianshuo Gao · Interface Developer

PROPOSAL: email and SMS notification module, user interface, task management; tests notification triggers jointly with Member 4.

- **Web UI and API:** `static/index.html`, `webapp.py`
- **Command-line task management:** `cli.py`
- **Email notifications:** `notifier.py` (Wenhan shows the alert email in the demo, as trigger logic is tested jointly)
- **Tests:** `tests/test_webapp.py`, `tests/test_cli.py`, `tests/test_notifier.py`

## Xiao Zhang · Agent Developer & Test

PROPOSAL: LLM natural-language parsing, autonomous monitoring loop and scheduling (with Member 5); system testing and accuracy evaluation.

- **Find agents:** `agents/__init__.py`, `agents/understand.py`, `agents/rewrite.py`, `agents/select.py`, `agents/describe.py`, with `prompts/understand.md`, `prompts/rewrite.md`, `prompts/select.md`, `prompts/describe.md`
- **Same-product matching:** `matching.py`, `agents/offer_verifier.py`, `prompts/verify_offers.md`
- **Tests:** `tests/test_agents.py`, `tests/test_ai_features.py`, `tests/test_pipeline.py`, `tests/test_matching.py`
- **Evaluation (with Fengwei and Wenhan):** `evaluation/__init__.py`, `evaluation/datasets.py`

## Wenhan Zhang · Agent Developer & Test

PROPOSAL: LLM natural-language parsing, autonomous monitoring loop and scheduling (with Member 4); system testing and accuracy evaluation.

- **Monitoring and deal decisions:** `scheduler.py` (scheduled checks, deal rules, send permits, one email per drop)
- **Instruction parsing and price reading:** `agents/task_parser.py`, `agents/price_extractor.py`, `agents/deal_summary.py`, with `prompts/parse_instruction.md`, `prompts/extract_price_from_page.md`, `prompts/summarise_deal.md`
- **Output validation:** `schemas.py`
- **Tests:** `tests/test_scheduler.py`, `tests/test_scheduler_multi_source.py`, `tests/test_monitoring.py`, `tests/test_parser.py`, `tests/test_schemas.py`, `tests/test_demo.py`
- **Evaluation (with Fengwei and Xiao):** `evaluation/runner.py`, `evaluation/__main__.py`, `tests/test_evaluation.py`

## Shared

- `docs/EVALUATION.md` collects all three accuracy measures: Gang provides the real price sample, Xiao and Wenhan the agent and alert cases, and Fengwei consolidates the results.
- Interfaces between modules are described in [docs/INTERFACES.md](INTERFACES.md); a change to one of them is agreed with the owners on both sides.
