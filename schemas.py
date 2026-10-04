"""Local validation of untrusted model output, request schema v1."""

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


SEARCH_FIELDS = {
    "schema_version", "status", "product_name", "url", "budget", "currency",
    "colour", "capacity", "condition", "comparison", "frequency_minutes",
    "missing_fields", "clarification_question",
}
#: A search may legitimately start without a URL, which is the whole point of Stage B.
SEARCH_REQUIRED = ("product_name",)
#: An unstated check frequency falls back to this cadence: once per day.
DEFAULT_FREQUENCY_MINUTES = 1440
MAX_SEARCH_QUERY_CHARS = 120
MAX_CONSTRAINT_CHARS = 40


def validate_search_request(data, instruction):
    """Validate schema v2: a *search* request, not a confirmed monitor task.

    Kept separate from `validate_task` on purpose. A search produces candidates for
    the user to choose from; it is not permission to monitor anything, so it does not
    require a URL and it never creates a task by itself.
    """
    if not isinstance(data, dict) or set(data) != SEARCH_FIELDS:
        raise ValueError("Unexpected fields")
    request = dict(data)
    if request["schema_version"] != "2" or request["status"] not in (
        "ready", "needs_clarification", "unsupported",
    ):
        raise ValueError("Invalid version/status")

    for field in ("product_name", "url", "currency", "colour", "capacity",
                  "condition", "comparison", "clarification_question"):
        value = request[field]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("Expected nonempty string or null")
    if request.get("product_name") and len(request["product_name"]) > MAX_SEARCH_QUERY_CHARS:
        raise ValueError("Product name is too long to search")
    for field in ("colour", "capacity"):
        if request.get(field) and len(request[field]) > MAX_CONSTRAINT_CHARS:
            raise ValueError(f"{field} is too long")
        if request.get(field) and len(instruction) and request[field].lower() not in instruction.lower():
            # The model must not invent a constraint the user never stated.
            raise ValueError(f"{field} was not supplied by the user")

    if request["currency"] not in (None,) + SUPPORTED_CURRENCIES:
        raise ValueError("Unsupported currency")
    if request["comparison"] not in (None, "lt", "lte"):
        raise ValueError("Unsupported comparison")
    if request["condition"] not in (None, "new", "used", "refurbished", "any"):
        raise ValueError("Unsupported condition")

    budget = request["budget"]
    if budget is not None:
        if not isinstance(budget, str) or not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", budget) or Decimal(budget) <= 0:
            raise ValueError("Invalid budget amount")
        request["budget"] = str(Decimal(budget).quantize(Decimal("0.01")))
        if request["currency"] is None:
            # A budget without a currency cannot be compared against a shop price.
            request["missing_fields"] = list(
                dict.fromkeys(list(request["missing_fields"]) + ["currency"])
            )

    frequency = request["frequency_minutes"]
    if frequency is not None and (type(frequency) is not int or not 5 <= frequency <= 525600):
        raise ValueError("Frequency must be 5..525600 whole minutes")
    if frequency is None:
        # Checking frequency is optional: a user who never names a cadence is not asked
        # about one, so an unstated frequency defaults to once per day (1440 minutes).
        # Explicit values are still held to the 5..525600 range checked above.
        frequency = DEFAULT_FREQUENCY_MINUTES
        request["frequency_minutes"] = frequency

    url = request["url"]
    if url is not None:
        supplied = {u.rstrip(".,;:!?)]\u3002\uff0c\uff1b\uff01\uff1f\uff09\u300b") for u in re.findall(r"https?://[!-~]+", instruction)}
        if url not in supplied:
            raise ValueError("URL was not supplied by user")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("https", "http") or not host or parsed.username or parsed.password:
            raise ValueError("Invalid URL")
        if "." not in host or host.endswith((".local", ".localhost", ".internal")) or parsed.port not in (None, 80, 443):
            raise ValueError("Nonpublic URL")

    missing = request["missing_fields"]
    if not isinstance(missing, list) or any(
        not isinstance(f, str)
        or f not in SEARCH_REQUIRED + ("currency", "budget", "frequency_minutes")
        for f in missing
    ):
        raise ValueError("Invalid missing fields")
    # frequency_minutes is never a genuine gap now: the default above already stands in
    # for an unstated cadence, so it is dropped even when the model still reports it.
    missing = [f for f in missing if f != "frequency_minutes"]

    if request["status"] == "unsupported":
        if not request["clarification_question"]:
            raise ValueError("Unsupported requests need an explanation")
        return request

    missing = list(dict.fromkeys([f for f in SEARCH_REQUIRED if request[f] is None] + missing))
    request["missing_fields"] = missing
    if missing or request["status"] == "needs_clarification":
        request["status"] = "needs_clarification"
        if not missing and not request["clarification_question"]:
            raise ValueError("Missing clarification question")
        request["clarification_question"] = request["clarification_question"] or (
            "请补充或明确：" + ", ".join(missing)
        )
    else:
        request["clarification_question"] = None
    return request


def validate_quote(quote: dict) -> dict:
    """Normalise and validate a price quote dict.

    A usable quote must have a finite, positive price, a supported currency,
    a fetch timestamp and a clear in_stock value (True/False/None). Ambiguous
    or cross-product splicing must be rejected before it reaches comparison.
    """
    if not isinstance(quote, dict):
        raise ValueError("Quote must be a dict")
    required = ("product_name", "url", "retailer", "price", "currency", "fetched_at", "in_stock")
    missing = [f for f in required if f not in quote or quote[f] is None and f != "in_stock"]
    if missing:
        raise ValueError(f"Quote missing fields: {missing}")
    try:
        price = Decimal(quote["price"])
    except Exception as exc:
        raise ValueError(f"Quote price is not a valid decimal: {quote['price']!r}") from exc
    if price != price or price == Decimal("Infinity") or price == Decimal("-Infinity"):
        raise ValueError("Quote price must be finite")
    if price <= 0:
        raise ValueError("Quote price must be greater than zero")
    quote["price"] = str(price.quantize(Decimal("0.01")))
    currency = str(quote["currency"]).upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError(f"Unsupported quote currency: {currency}")
    quote["currency"] = currency
    if quote.get("in_stock") not in (True, False, None):
        raise ValueError("Quote in_stock must be True, False or None")
    if not isinstance(quote.get("fetched_at"), str) or not quote["fetched_at"].strip():
        raise ValueError("Quote fetched_at is required")
    return quote


PRICE_EXTRACTION_FIELDS = {"product_name", "price", "currency", "in_stock"}
#: Optional identity attributes the LLM may also state from the page text. They are
#: never required: a page that states no brand/colour/capacity still yields a valid
#: price. When present they help `discovery._detail_identity` confirm a web candidate
#: as the same product, but they never override a different verdict there.
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
