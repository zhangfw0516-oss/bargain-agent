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

See [分阶段开发与验收计划](DEVELOPMENT_PLAN.md) for phase checklists, team responsibilities, acceptance criteria, progress tracking, and the proposed DeepSeek integration. The plan describes pending work, not implemented features.

## Quick Start

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate   # Linux/macOS
venv\Scripts\activate      # Windows

# Install dependencies
pip install -r requirements.txt

# Copy and edit environment variables
cp .env.example .env

# Run pipeline demo
python main.py --mock-demo
```

## Project Structure

### Stage 1: LLM parser (development)

For DeepSeek, configure your local `.env` with `LLM_API_BASE_URL=https://api.deepseek.com`, your `LLM_API_KEY`, and an available `LLM_MODEL` from the [official documentation](https://api-docs.deepseek.com/zh-cn/). No live-provider accuracy has been verified yet.

```bash
python main.py --instruction "Monitor headphones https://example.com/item below NZD 200 every hour"
python -m unittest discover -s tests -v
```

The URL is illustrative. Parsing prints a validated request or clarification question; it does not fetch websites, create tasks, or send email. Answer clarifications by submitting a new complete instruction; multi-turn dialogue is not implemented. `--mock-demo` is entirely simulated.

Prompt: `prompts/parse_instruction.md`; validation: `schemas.py`. Supported currencies: NZD/AUD/USD; minimum interval: 5 minutes. Tests use fake clients without API charges. JSON validation does not prove semantic accuracy; live evaluation is still required. URL validation is preliminary: future fetchers must check retailer allowlists, DNS addresses and redirects before network access. API timeout: 30 seconds with one SDK retry; malformed output returns an error, never Mock success.

```
bargain-agent/
├── agent.py          # LLM instruction parsing & task scheduling
├── scraper.py        # Web scraping (static + dynamic)
├── notifier.py       # Email & SMS notifications
├── main.py           # Pipeline entry point
├── requirements.txt  # Python dependencies
├── .env.example      # Environment variable template
└── PROPOSAL.md       # Full project proposal
```
