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
python main.py
```

## Project Structure

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
