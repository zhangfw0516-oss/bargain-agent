"""Local validation of untrusted model output, request schema v1."""

from decimal import Decimal
import ipaddress
import re
from urllib.parse import urlsplit

FIELDS = {"schema_version", "status", "product_name", "url", "target_price",
          "currency", "comparison", "frequency_minutes", "condition",
          "missing_fields", "clarification_question"}
REQUIRED = ("product_name", "url", "target_price", "currency", "comparison", "frequency_minutes")


def validate_task(data, instruction):
    if not isinstance(data, dict) or set(data) != FIELDS:
        raise ValueError("Unexpected fields")
    task = dict(data)
    if task["schema_version"] != "1" or task["status"] not in ("ready", "needs_clarification", "unsupported"):
        raise ValueError("Invalid version/status")
    for field in ("product_name", "url", "currency", "comparison", "condition", "clarification_question"):
        value = task[field]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("Expected nonempty string or null")
    if task["currency"] not in (None, "NZD", "AUD", "USD"):
        raise ValueError("Unsupported currency")
    if task["comparison"] not in (None, "lt", "lte"):
        raise ValueError("Unsupported comparison")
    if task["condition"] not in (None, "new", "used", "refurbished", "any"):
        raise ValueError("Unsupported condition")
    price = task["target_price"]
    if price is not None:
        if not isinstance(price, str) or not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", price) or Decimal(price) <= 0:
            raise ValueError("Invalid decimal amount")
        task["target_price"] = str(Decimal(price).quantize(Decimal("0.01")))
    frequency = task["frequency_minutes"]
    if frequency is not None and (type(frequency) is not int or not 5 <= frequency <= 525600):
        raise ValueError("Frequency must be 5..525600 whole minutes")
    url = task["url"]
    if url is not None:
        supplied = {u.rstrip(".,;!?，。；！？”）)") for u in re.findall(r'https?://[^\s<>"\']+', instruction)}
        if url not in supplied:
            raise ValueError("URL was not supplied by user")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("https", "http") or not host or parsed.username or parsed.password:
            raise ValueError("Invalid URL")
        if "." not in host or host.endswith((".local", ".localhost", ".internal")) or parsed.port not in (None, 80, 443):
            raise ValueError("Nonpublic URL")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("Nonpublic IP")
    missing = task["missing_fields"]
    if not isinstance(missing, list) or any(not isinstance(f, str) or f not in REQUIRED + ("condition",) for f in missing):
        raise ValueError("Invalid missing fields")
    if task["status"] == "unsupported":
        if not task["clarification_question"]:
            raise ValueError("Unsupported requests need an explanation")
        return task
    missing = list(dict.fromkeys([f for f in REQUIRED if task[f] is None] + missing))
    task["missing_fields"] = missing
    if missing or task["status"] == "needs_clarification":
        task["status"] = "needs_clarification"
        if not missing and not task["clarification_question"]:
            raise ValueError("Missing clarification question")
        task["clarification_question"] = task["clarification_question"] or "请补充或明确：" + ", ".join(missing)
    else:
        task["clarification_question"] = None
    return task
