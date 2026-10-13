"""Email notification with SMTP TLS/SSL, timeouts and limited retries."""

from __future__ import annotations

import logging
import os
import smtplib
import socket
import ssl
import time
from email.message import EmailMessage
from typing import Any

import config

logger = logging.getLogger(__name__)


class NotifierError(RuntimeError):
    """Raised when a notification cannot be delivered."""


# TLS/certificate and capability problems are configuration issues, not transient.
PERMANENT_TLS_ERRORS = (ssl.SSLError, smtplib.SMTPNotSupportedError)


def _is_retryable_error(exc: Exception) -> bool:
    """Return True if the error is transient and another attempt may succeed.

    Classification order matters:
    1. RecipientsRefused carries per-recipient codes, so it is inspected first.
    2. Any exception with an SMTP status code is judged by 4xx (transient) vs
       5xx (permanent) — a 454 authentication pause or a 450 sender/recipient
       deferral must not end the event as if it were a hard failure.
    3. TLS/certificate problems stay permanent.
    4. Transient network conditions are retryable.
    5. Anything unrecognised is treated as permanent, so an unknown fault does
       not make us hammer the provider within the same budget.
    """
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _message in exc.recipients.values() if isinstance(code, int)]
        if codes:
            return any(400 <= code < 500 for code in codes)
        return False

    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int):
        if 400 <= code < 500:
            return True
        if 500 <= code < 600:
            return False

    # Checked before the generic OSError branch because ssl.SSLError subclasses OSError.
    if isinstance(exc, PERMANENT_TLS_ERRORS):
        return False

    if isinstance(
        exc,
        (socket.timeout, socket.gaierror, ConnectionError, OSError, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected),
    ):
        return True

    return False


def _sanitize_error(exc: Exception) -> str:
    """Return a log-safe error string that does not include credentials."""
    text = str(exc)
    # Remove likely credential substrings from error messages without
    # requiring config values to be present (tests may run without .env).
    for env_var in ("SMTP_PASS", "SMTP_USER"):
        secret = os.getenv(env_var)
        if secret and secret in text:
            text = text.replace(secret, "***")
    return text


def send_email_notification(
    to_email: str,
    subject: str,
    body: str,
    *,
    from_email: str | None = None,
    max_retries: int | None = None,
) -> dict[str, Any]:
    """Send an email alert via SMTP and return a structured result.

    max_retries controls the number of *additional* attempts made inside this
    function. It defaults to 0 so that the scheduler owns the overall retry
    budget and notifier retries are not multiplied unexpectedly.
    """
    if not to_email or "@" not in to_email:
        raise NotifierError("Invalid recipient email address.")

    host = config.smtp_host()
    port = config.smtp_port()
    user = config.smtp_user()
    password = config.smtp_password()
    from_addr = from_email or config.smtp_from()
    timeout = config.smtp_timeout()
    max_retries = 0 if max_retries is None else max(0, max_retries)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_email
    msg.set_content(body)

    last_error = None
    retryable = True
    attempts = 0
    while attempts <= max_retries:
        attempts += 1
        server = None
        try:
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=timeout)
            else:
                server = smtplib.SMTP(host, port, timeout=timeout)
                server.starttls()
            with server:
                server.login(user, password)
                server.send_message(msg)
            logger.info("Email sent to %s (attempt %s)", to_email, attempts)
            return {"success": True, "attempts": attempts, "error": None, "retryable": False}
        except Exception as exc:
            retryable = _is_retryable_error(exc)
            last_error = _sanitize_error(exc)
            logger.warning("Email attempt %s failed: %s (retryable=%s)", attempts, last_error, retryable)
            # Ensure the connection is closed on TLS/starttls/send failures.
            if server is not None:
                try:
                    server.close()
                except Exception:
                    pass
            if not retryable:
                break
            if attempts <= max_retries:
                time.sleep(1.5 * attempts)

    return {"success": False, "attempts": attempts, "error": last_error, "retryable": retryable}


def deal_subject(task: dict[str, Any], quote: dict[str, Any]) -> str:
    """A subject that stands out even when the sender is the recipient ("me")."""
    name = " ".join(str(task.get("product_name") or "").split())
    if len(name) > 60:
        name = name[:57].rstrip() + "..."
    price = f"{quote.get('currency')} {quote.get('price')}"
    shop = f" @ {quote.get('retailer')}" if quote.get("retailer") else ""
    return f"[Bargain Agent] 降价提醒：{name} — {price}{shop}"


def insight_lines(task: dict[str, Any], quote: dict[str, Any]) -> list[str]:
    """One line of price history context, when there is enough history."""
    insight = quote.get("insight")
    if not insight:
        return []
    currency = task.get("currency")
    if insight.get("is_lowest"):
        verdict = f"Lowest price in the last {insight['days']} days"
    elif insight.get("pct_vs_average") is not None and insight["pct_vs_average"] < 0:
        verdict = f"{abs(insight['pct_vs_average']):g}% below the {insight['days']}-day average"
    else:
        verdict = f"Not below the {insight['days']}-day average"
    return [f"Price history: {verdict} (low {currency} {insight['lowest']}, average {currency} "
            f"{insight['average']}, {insight['checks']} checks)."]


def build_deal_body(task: dict[str, Any], quote: dict[str, Any]) -> str:
    """Deterministic plain-text body for a deal alert (the non-LLM fallback)."""
    headline = (
        f"降价提醒 / Price alert: {quote.get('retailer')} 现价 {quote.get('currency')} {quote.get('price')}"
        f"（目标 {task['currency']} {task['target_price']}）"
    )
    lines = [
        headline,
        "",
        "A price match was found for your monitored item.",
        "",
        f"Product: {quote.get('product_name')}",
        f"Current price: {quote.get('currency')} {quote.get('price')}",
        f"Your threshold: {task['currency']} {task['target_price']} ({task['comparison']})",
        *([f"Price drop: at least {task.get('drop_percent')}% below {task['currency']} {quote.get('reference_price')}"]
          if quote.get("deal_reason") == "drop" else []),
        *insight_lines(task, quote),
        f"Source: {quote.get('url')}",
        f"Retailer: {quote.get('retailer')}",
        f"In stock: {quote.get('in_stock')}",
        f"Checked at: {quote.get('fetched_at')}",
    ]
    shipping = quote.get("shipping")
    if shipping in (None, ""):
        lines.append("Shipping cost: unknown (price does not include verified shipping).")
    else:
        try:
            from decimal import Decimal

            total = (Decimal(str(quote.get("price"))) + Decimal(str(shipping))).quantize(Decimal("0.01"))
            lines.append(f"Shipping: {quote.get('currency')} {shipping} — delivered total {quote.get('currency')} {total}"
                         + (" (compared)" if task.get("include_shipping") else ""))
        except Exception:  # noqa: BLE001
            lines.append(f"Shipping: {quote.get('currency')} {shipping}")

    # A task can watch several shops. Report the others explicitly instead of
    # leaving the reader to assume they were all fine.
    others = quote.get("other_sources") or []
    if others:
        lines.append("")
        lines.append(f"Other monitored sources ({len(others)}):")
        for other in others:
            label = other.get("retailer") or other.get("url") or "unknown source"
            lines.append(f"  - {label}: {other.get('state')}")
        lines.append("The price above is the best usable offer found at the time of this check.")
    return "\n".join(lines)


def _llm_deal_body(task: dict[str, Any], quote: dict[str, Any]) -> str | None:
    """Best-effort LLM summary; returns None (caller falls back) on any failure."""
    from agents import deal_summary

    try:
        summary = deal_summary.run(task, quote)
    except Exception:  # noqa: BLE001 - a summary must never block the alert
        return None
    if not summary or not summary.strip():
        return None
    lines = [
        summary.strip(),
        "",
        f"Product: {quote.get('product_name')}",
        f"Current price: {quote.get('currency')} {quote.get('price')}",
        f"Source: {quote.get('url')}",
    ]
    return "\n".join(lines)


def notify_deal(to_email: str, task: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    """Build and send a deal-alert email.

    The scheduler controls the overall retry budget; this function makes a single
    SMTP attempt (max_retries=0 inside notifier) so retries are not duplicated.

    When `LLM_EMAIL_SUMMARY` is enabled, the body starts with a short natural-language
    summary of the best offer and any failed/unknown sources. The deterministic body is
    always the fallback, so an LLM failure can never suppress the alert.
    """
    subject = deal_subject(task, quote)
    body = build_deal_body(task, quote)
    if config.llm_email_summary():
        enhanced = _llm_deal_body(task, quote)
        if enhanced:
            body = enhanced
    return send_email_notification(to_email, subject, body, max_retries=0)
