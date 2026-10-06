"""First-run configuration: minimal setup entry for the distributable package.

Reads/writes the app-local ``.env`` next to ``config.py``. Values are never
returned to the UI — only which required keys are still missing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

#: Keys a fresh installation must fill in before live mode can work.
REQUIRED_KEYS = (
    "DEFAULT_NOTIFY_EMAIL",  # where deal notifications are sent
    "SMTP_HOST",             # outgoing mail server, e.g. smtp.gmail.com
    "SMTP_PORT",             # usually 465 or 587
    "SMTP_USER",             # sender account
    "SMTP_PASS",             # app-specific password (NOT the login password)
    "LLM_API_KEY",           # OpenAI-compatible model key (DeepSeek by default)
    "LLM_API_BASE_URL",
    "LLM_MODEL",
)

#: Google Shopping search: at least one of these (Serper is primary, SerpApi the backup).
SEARCH_KEYS = ("SERPER_API_KEY", "SERPAPI_API_KEY")

#: Keys the setup form may write.
WRITABLE_KEYS = REQUIRED_KEYS + ("SMTP_FROM",)

#: SMTP-only subset (everything except the recipient and the LLM keys).
SMTP_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS")

#: LLM subset.
LLM_KEYS = ("LLM_API_KEY", "LLM_API_BASE_URL", "LLM_MODEL")

#: Page extractors for pasted URLs; at least one is needed for URL sources.
EXTRACTOR_KEYS = ("TAVILY_API_KEY", "FIRECRAWL_API_KEY", "SCRAPEDO_API_KEY")

#: Test/hermetic override: when this environment variable is set, all setup
#: I/O stays inside the given file and the process environment is never
#: consulted or mutated (so tests can never touch the developer's real .env).
ENV_FILE_OVERRIDE_VAR = "BARGAIN_AGENT_ENV_FILE"


def env_path() -> Path:
    override = os.environ.get(ENV_FILE_OVERRIDE_VAR)
    if override:
        return Path(override)
    import config

    return config.env_file()


def _isolated() -> bool:
    return bool(os.environ.get(ENV_FILE_OVERRIDE_VAR))


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        values[key.strip()] = raw.strip().strip('"').strip("'")
    return values


def _present(key: str, file_values: dict[str, str]) -> bool:
    # os.environ first (already-loaded config wins), then the file. In isolated
    # mode (tests / hermetic runs) only the file is consulted.
    if not _isolated() and os.getenv(key, "").strip():
        return True
    return bool(file_values.get(key, "").strip())


def status() -> dict[str, Any]:
    """Which required keys are missing? Never includes any value."""
    file_values = _read_env_file(env_path())
    missing = [key for key in REQUIRED_KEYS if not _present(key, file_values)]
    search_ready = any(_present(key, file_values) for key in SEARCH_KEYS)
    if not search_ready:
        missing.append(" or ".join(SEARCH_KEYS))
    smtp_missing = [key for key in SMTP_KEYS if not _present(key, file_values)]
    llm_missing = [key for key in LLM_KEYS if not _present(key, file_values)]
    return {
        "complete": not missing,
        "smtp_ready": not smtp_missing,
        "llm_ready": not llm_missing,
        "search_ready": search_ready,
        "extract_ready": any(_present(key, file_values) for key in EXTRACTOR_KEYS),
        "missing": missing,
    }


def write_updates(updates: dict[str, str]) -> None:
    """Merge ``updates`` into the app-local .env, preserving all other lines."""
    path = env_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    # Make the values effective for the running process immediately (no restart
    # needed). Values are never echoed back or logged. In isolated mode the
    # process environment is left untouched.
    if _isolated():
        return
    for key, value in updates.items():
        os.environ[key] = value
