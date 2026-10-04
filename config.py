"""Environment configuration loader.

Raises actionable ConfigError when required settings are missing.
Never prints secrets.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:  # distributable package: stdlib-only fallback
    def load_dotenv(env_path, **_kwargs):  # type: ignore[misc]
        for line in Path(env_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            os.environ.setdefault(key.strip(), raw.strip().strip('"').strip("'"))

_ENV_LOADED = False


class ConfigError(RuntimeError):
    """Missing or invalid configuration."""


def _load() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    env_path = Path(__file__).with_name(".env")
    if env_path.exists():
        load_dotenv(env_path)
    _ENV_LOADED = True


def _get(name: str) -> str:
    _load()
    value = os.getenv(name, "").strip()
    return value


def require(name: str, guidance: str) -> str:
    value = _get(name)
    if not value:
        raise ConfigError(f"Missing {name}. {guidance}")
    return value


def llm_api_key() -> str:
    return require(
        "LLM_API_KEY",
        "Set LLM_API_KEY in .env (copied from .env.example). Do not commit it.",
    )


def llm_base_url() -> str:
    return require(
        "LLM_API_BASE_URL",
        "Set LLM_API_BASE_URL in .env, e.g. https://api.deepseek.com for DeepSeek.",
    )


def llm_model() -> str:
    return require(
        "LLM_MODEL",
        "Set LLM_MODEL in .env to an available model ID from your provider.",
    )


def _agent_override(base: str, agent: str | None) -> str:
    if agent:
        value = _get(f"{base}_{agent.upper()}")
        if value:
            return value
    return _get(base)


def llm_settings(agent: str | None = None) -> tuple[str, str, str]:
    """(api_key, base_url, model) for one agent.

    ``LLM_API_KEY_<AGENT>`` / ``LLM_API_BASE_URL_<AGENT>`` / ``LLM_MODEL_<AGENT>``
    override the global ``LLM_*`` values, so e.g. the select agent can use a
    stronger model than the rewrite agent.
    """
    values = []
    for base, guidance in (
        ("LLM_API_KEY", "Set LLM_API_KEY in .env (copied from .env.example). Do not commit it."),
        ("LLM_API_BASE_URL", "Set LLM_API_BASE_URL in .env, e.g. https://api.deepseek.com."),
        ("LLM_MODEL", "Set LLM_MODEL in .env to an available model ID from your provider."),
    ):
        value = _agent_override(base, agent)
        if not value:
            raise ConfigError(f"Missing {base}. {guidance}")
        values.append(value)
    return values[0], values[1], values[2]


def _finite_positive_float(raw: str, default: str, name: str) -> float:
    try:
        value = float(raw or default)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value != value or value == float("inf") or value == float("-inf"):
        raise ConfigError(f"{name} must be finite")
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value


def llm_timeout() -> float:
    return _finite_positive_float(_get("LLM_TIMEOUT"), "30.0", "LLM_TIMEOUT")


def smtp_host() -> str:
    return require("SMTP_HOST", "Set SMTP_HOST in .env.")


def smtp_port() -> int:
    raw = require("SMTP_PORT", "Set SMTP_PORT in .env.")
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError("SMTP_PORT must be an integer") from exc


def smtp_user() -> str:
    return require("SMTP_USER", "Set SMTP_USER in .env.")


def smtp_password() -> str:
    return require(
        "SMTP_PASS",
        "Set SMTP_PASS in .env (app password, not your login password for Gmail).",
    )


def smtp_timeout() -> float:
    return _finite_positive_float(_get("SMTP_TIMEOUT"), "10.0", "SMTP_TIMEOUT")


def smtp_from() -> str:
    return _get("SMTP_FROM") or smtp_user()


def notification_max_retries() -> int:
    try:
        value = int(_get("NOTIFICATION_MAX_RETRIES") or "3")
    except ValueError as exc:
        raise ConfigError("NOTIFICATION_MAX_RETRIES must be an integer") from exc
    if value < 0 or value > 20:
        raise ConfigError("NOTIFICATION_MAX_RETRIES must be between 0 and 20")
    return value


def database_url(default: str = "sqlite:///bargain_agent.db") -> str:
    _load()
    return os.getenv("DATABASE_URL", default).strip() or default


def database_path() -> Path:
    url = database_url()
    prefix = "sqlite:///"
    if url.startswith(prefix):
        return Path(url[len(prefix) :]).resolve()
    raise ConfigError("Only sqlite:/// URLs are supported")


def _normalise_domain(domain: str) -> str:
    d = domain.strip().lower()
    if d.startswith("www."):
        d = d[4:]
    return d


#: Shops with an adapter in retailers/. This is the supported-shop boundary, not a crawl
#: scope: 每新增一家商家都要显式加入这里并配套一个适配器。
#: Mighty Ape is listed although its site refuses automated requests (DataDome 403); it
#: stays so the UI can report "not searched" honestly instead of omitting the shop.
DEFAULT_ALLOWED_DOMAINS = ("pbtech.co.nz", "mightyape.co.nz", "jbhifi.co.nz")


def allowed_scraper_domains() -> list[str]:
    raw = _get("SCRAPER_ALLOWED_DOMAINS")
    if raw:
        return [_normalise_domain(d) for d in raw.split(",") if d.strip()]
    return list(DEFAULT_ALLOWED_DOMAINS)


def scraper_timeout() -> float:
    return _finite_positive_float(_get("SCRAPER_TIMEOUT"), "15.0", "SCRAPER_TIMEOUT")


def scraper_max_response_bytes() -> int:
    try:
        value = int(_get("SCRAPER_MAX_RESPONSE_BYTES") or "10_000_000")
    except ValueError as exc:
        raise ConfigError("SCRAPER_MAX_RESPONSE_BYTES must be an integer") from exc
    if value <= 0 or value > 100_000_000:
        raise ConfigError("SCRAPER_MAX_RESPONSE_BYTES must be between 1 and 100000000")
    return value


def dry_run() -> bool:
    return _get("DRY_RUN").lower() in ("1", "true", "yes")


def llm_email_summary() -> bool:
    """Whether deal emails may use an LLM-generated natural-language summary.

    Off by default: the deterministic template remains the normal path, and turning
    this on makes one bounded LLM call per alert attempt. A failure falls back to the
    template, so the alert is never lost because the summary could not be generated.
    """
    return _get("LLM_EMAIL_SUMMARY").lower() in ("1", "true", "yes")

def llm_explain_results() -> bool:
    # Off by default; deterministic reasons stay the normal rendering.
    # On, one bounded LLM call per needs_confirmation/unavailable row;
    # failure always falls back to the deterministic reasons.
    return _get("LLM_EXPLAIN_RESULTS").lower() in ("1", "true", "yes")


def llm_query_expansion() -> bool:
    """Whether the LLM may expand the search query into Brave variants.
    Off by default; the original query is always used as the first query.
    A failure returns only the original query."""
    return _get("LLM_QUERY_EXPANSION").lower() in ("1", "true", "yes")


def llm_candidate_ranking() -> bool:
    """Whether the LLM may re-rank candidates by relevance before detail-fetching.
    Off by default; original order is preserved on failure. Never removes candidates."""
    return _get("LLM_CANDIDATE_RANKING").lower() in ("1", "true", "yes")


def llm_match_assist() -> bool:
    """Whether the LLM may assist colour/accessory matching when deterministic
    logic cannot decide. Off by default; deterministic hardcoded rules stay the
    primary path. A failure always falls back to the deterministic result."""
    return _get("LLM_MATCH_ASSIST").lower() in ("1", "true", "yes")


def llm_concreteness() -> bool:
    """Whether the master AI judges each discovered item as a concrete product.

    On by default whenever the intelligent-find flow is enabled (and a model is
    configured): without it a guide/article/category page can be recommended as a
    "product". Set ``LLM_CONCRETENESS=false`` to force the keyword fallback.
    A judgement failure always falls back to the deterministic heuristic.
    """
    raw = _get("LLM_CONCRETENESS").lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    # Default-on only when the find flow is enabled AND a model is actually
    # configured; otherwise an AI judgement that can never succeed would filter
    # out every recommendation (offline tests and no-key runs included).
    return llm_find_enabled() and bool(_get("LLM_API_KEY") and _get("LLM_MODEL"))


def llm_find_enabled() -> bool:
    """Whether the descriptive "intelligent find" AI-discovery flow is available.

    Off by default: the deterministic parse->search->confirm flow stays the normal
    path, and enabling this makes discovery spend real model + web-search calls.
    """
    return _get("LLM_FIND_ENABLED").lower() in ("1", "true", "yes")
