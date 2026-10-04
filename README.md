# Bargain Agent

An agentic AI system to monitor bargain deals of retailers — 159.333 Computer Science Project, Massey University, Semester 2 2026.

## Team

| Role | Name | Email |
| :--- | :--- | :--- |
| Project Lead | Fengwei Zhang (24009491) | Zephyri.fw@gamil.com |
| Data Engineer | Gang Zhao (24009365) | 3132057704@qq.com |
| Interface Developer | Tianshuo Gao (24009350) | 3084083988@qq.com |
| Agent Dev & Test | Xiao Zhang (24009323) | 274751389@qq.com |
| Agent Dev & Test | Wenhan Zhang (24009400) | 3384155536@qq.com |

## Development Plan

**当前阶段 / Current work:** [每位成员的阶段 01 任务](docs/stages/01-foundation.md)。[阶段索引与滚动复盘流程](docs/stages/README.md) · [下一阶段模板](docs/stages/TEMPLATE.md)。

See [分阶段开发与验收计划](DEVELOPMENT_PLAN.md) for phase checklists, team responsibilities, acceptance criteria, progress tracking, and the proposed DeepSeek integration. The plan describes pending work, not implemented features.

## Quick Start

### For non-technical users (Windows)

Share `dist/bargain-agent-windows.zip` (built with `python tools_build_package.py`).
The recipient unzips, double-clicks **`Start Bargain Agent.bat`**, and the local web
page opens. Demo mode works with zero setup; the **Setup** section of the page
collects the recipient email, SMTP sender details + app password, and the DeepSeek
API key for live mode. See the README inside the package (bilingual) for the full
path and the limitations (no monitoring while the PC sleeps or the app is closed).

### For developers

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate   # Linux/macOS
venv\Scripts\activate      # Windows

# Install dependencies
pip install -r requirements.txt

# Copy and edit environment variables
cp .env.example .env

# Run offline tests (no network, LLM or SMTP required)
python -m unittest discover -s tests -v

# Local web interface — demo mode by default (offline parser, demo shops,
# fake notifications). Use --live for real shops / parsing / email.
python webapp.py --port 8765

# Add a monitoring task (requires LLM credentials). Shows a summary and asks for confirmation.
python main.py add --email me@example.com "Monitor https://www.pbtech.co.nz/product/X when below NZD 300"

# List tasks
python main.py list

# Run one check
python main.py check 1

# Start the scheduler loop
python main.py run
```

## Project Structure

```
bargain-agent/
├── agent.py             # LLM instruction parsing
├── webapp.py            # Local web interface (stdlib http.server, demo/live)
├── config_setup.py      # First-run configuration (writes app-local .env)
├── demo_data.py         # Offline demo parser/shops/fake notifier (web demo)
├── scraper.py           # Web scraping (PB Tech JSON-LD)
├── notifier.py          # Email notifications (SMTP)
├── storage.py           # SQLite persistence
├── scheduler.py         # Periodic price checks
├── cli.py               # Command-line interface
├── main.py              # CLI entry point
├── schemas.py           # Request/quote validation
├── config.py            # Environment configuration
├── static/              # Web UI (single-page, English)
├── prompts/             # Versioned LLM prompts
├── retailers/           # Shop adapters (PB Tech, JB Hi-Fi, Mighty Ape)
├── tools_build_package.py  # Builds the Windows one-click package into dist/
├── dist/                # Distributable package (zip) — generated
├── tests/               # Offline test suite
├── docs/                # Architecture & milestone docs
├── project_progress/    # Status, decisions, handoff
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template
└── PROPOSAL.md          # Full project proposal
```

## Milestone Docs

- [M40 — Real, Recoverable Price Monitoring Foundation](docs/M40.md)
- [Module Interfaces](docs/INTERFACES.md)
- [System Context](docs/SYSTEM_CONTEXT.md)

