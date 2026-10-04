"""Local validation of untrusted model output: monitoring tasks and page price extractions."""

from decimal import Decimal
import ipaddress
import re
from urllib.parse import urlsplit

FIELDS = {"schema_version", "status", "product_name", "url", "target_price",
          "currency", "comparison", "frequency_minutes", "condition",
          "missing_fields", "clarification_question"}
REQUIRED = ("product_name", "url", "target_price", "currency", "comparison", "frequency_minutes")
SUPPORTED_CURRENCIES = ("NZD", "AUD", "USD")


def validate_task(data, instruction):
    """Validate a parsed instruction dict and normalise fields."""
    if not isinstance(data, dict) or set(data) != FIELDS:
        raise ValueError("Unexpected fields")
    task = dict(data)
    if task["schema_version"] != "1" or task["status"] not in ("ready", "needs_clarification", "unsupported"):
        raise ValueError("Invalid version/status")
    for field in ("product_name", "url", "currency", "comparison", "condition", "clarification_question"):
        value = task[field]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("Expected nonempty string or null")
    if task["currency"] not in (None,) + SUPPORTED_CURRENCIES:
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
        # Match only printable ASCII after the scheme so that CJK punctuation or
        # text immediately following a URL does not get absorbed into it.
        supplied = {u.rstrip(".,;:!?)]}\u3002\uff0c\uff1b\uff01\uff1f\uff09\u300b") for u in re.findall(r"https?://[!-~]+", instruction)}
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


PRICE_EXTRACTION_FIELDS = {"product_name", "price", "currency", "in_stock"}
#: Optional identity attributes the LLM may also state from the page text. They are
#: never required: a page that states no brand/colour/capacity still yields a valid
#: price. When present they describe the page's variant for the quote.
PRICE_EXTRACTION_OPTIONAL = {"brand", "colour", "capacity"}


def validate_price_extraction(data):
    """Validate and normalise an LLM price extraction from a web page.

    Only a finite, positive price, a supported currency, a non-empty product name
    and a clear in_stock value pass. A missing or ambiguous price must fail closed
    rather than reach comparison, so an invented value never becomes an offer.

    ``brand``, ``colour`` and ``capacity`` are optional, never required. Each must be
    a non-empty string or null; an invented/empty value is dropped to null so it
    cannot confirm an identity the page did not state.
    """
    if not isinstance(data, dict):
        raise ValueError("Unexpected fields")
    keys = set(data)
    if not PRICE_EXTRACTION_FIELDS.issubset(keys):
        raise ValueError(f"Missing fields: {PRICE_EXTRACTION_FIELDS - keys}")
    unknown = keys - PRICE_EXTRACTION_FIELDS - PRICE_EXTRACTION_OPTIONAL
    if unknown:
        raise ValueError(f"Unexpected fields: {unknown}")
    out = dict(data)
    product_name = out["product_name"]
    if not isinstance(product_name, str) or not product_name.strip():
        raise ValueError("product_name must be a non-empty string")
    out["product_name"] = product_name.strip()
    price = out["price"]
    if not isinstance(price, str) or not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", price):
        raise ValueError("Invalid price amount")
    if Decimal(price) <= 0:
        raise ValueError("Price must be greater than zero")
    out["price"] = str(Decimal(price).quantize(Decimal("0.01")))
    currency = str(out["currency"]).upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError(f"Unsupported currency: {currency}")
    out["currency"] = currency
    if out["in_stock"] not in (True, False, None):
        raise ValueError("in_stock must be true, false or null")
    for field in PRICE_EXTRACTION_OPTIONAL:
        value = out.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{field} must be a non-empty string or null")
        out[field] = value.strip() if isinstance(value, str) else None
    return out
