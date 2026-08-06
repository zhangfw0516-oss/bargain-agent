# CLAUDE.md — Bargain Agent Project

## Project Context
- 159.333 Computer Science Project, Massey University, Semester 2 2026
- Supervisor: Dr Surangika Ranathunga
- An agentic AI system that monitors product prices across e-commerce retailers and alerts users on bargain deals.

## Tech Stack
- Python 3.11+
- Dependencies defined in `requirements.txt`
- Environment variables in `.env` (copy from `.env.example`)

## Code Conventions
- Functions use snake_case, classes use PascalCase.
- Each module is independent: `agent.py` (LLM parsing), `scraper.py` (web scraping), `notifier.py` (alerts).
- All public functions include type hints.
- Write short docstrings for module-level and function-level documentation.

## Git Workflow
- `main` branch — stable, always runnable.
- Feature branches: `feat/<short-description>`.
- Commit messages: concise, imperative mood (e.g., "add price parser for site X").
- Do NOT commit `.env` or secrets.
