"""Process-wide offline seal for the whole test suite.

The suite is meant to be fully offline: every test injects its own provider sender,
LLM client, observer and notifier. This module arms a recorder on each real outbound
boundary for the whole run and fails if anything reached one.

Because ``unittest discover`` imports every test module **before** running any test, a
single import-time patch covers the entire suite. The module is named ``test_zz_*`` so
its report runs after all others (``discover`` loads and runs modules in sorted order).

Sealed boundaries (the full current egress surface):

- DNS / low-level connect             — ``socket.getaddrinfo`` and
  ``socket.create_connection`` for any **non-loopback** host. The loopback is left
  open because the Stage D web suite runs a real HTTP server on ``127.0.0.1``; every
  other hostname or public IP is refused before a packet leaves.
- ``requests.Session``
- module-level HTTP verbs             — ``requests.get`` / ``requests.post`` /
  ``requests.request`` (the LLM client posts; provider senders use ``request``)
- SerpApi / Tavily / Firecrawl         — ``sources.base.default_sender``
- SMTP notification                    — ``smtplib.SMTP`` / ``smtplib.SMTP_SSL``
- OpenAI SDK, if installed             — ``openai.OpenAI``

A regression that reaches any of these is recorded and raised here, so a real key in
``.env`` can never turn an offline run into a live one unnoticed. The recorded label is
kept so the failure names the exact boundary the leak used; the raised
``OfflineNetworkViolation`` carries a stack trace to the offending call site.
"""

import socket
import smtplib
import unittest

import requests

import sources.base as provider_base


class OfflineNetworkViolation(AssertionError):
    """A test attempted a real outbound call; the suite must stay offline."""


_RECORDED: list[str] = []


def _record(label: str):
    def _blocked(*args, **kwargs):
        _RECORDED.append(label)
        raise OfflineNetworkViolation(
            f"The offline test suite attempted a real call through {label}. "
            "Inject a fake provider / fetch / sender instead."
        )

    _blocked.__offline_seal__ = label  # type: ignore[attr-defined]
    return _blocked


def _is_loopback(host) -> bool:
    """True for the loopback / wildcard hosts the local web server legitimately uses.

    ``None`` and an empty string are the ``AI_PASSIVE`` wildcard used for local binds,
    never a remote destination, so they are allowed through. Everything else must be a
    loopback name or address to pass.
    """
    if host is None:
        return True
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:  # pragma: no cover - non-ascii host bytes
            return False
    text = str(host).strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if text in ("", "localhost", "localhost.localdomain", "0.0.0.0", "::", "::1"):
        return True
    if text.startswith("127."):
        return True
    return False


_real_getaddrinfo = socket.getaddrinfo
_real_create_connection = socket.create_connection


def _sealed_getaddrinfo(host, *args, **kwargs):
    if not _is_loopback(host):
        _RECORDED.append(f"socket.getaddrinfo({host!r}) (DNS)")
        raise OfflineNetworkViolation(
            "The offline test suite attempted real DNS resolution for "
            f"{host!r}. Patch the resolver or inject a fake fetch instead."
        )
    return _real_getaddrinfo(host, *args, **kwargs)


def _sealed_create_connection(address, *args, **kwargs):
    host = address[0] if isinstance(address, (tuple, list)) and address else None
    if not _is_loopback(host):
        _RECORDED.append(f"socket.create_connection({address!r})")
        raise OfflineNetworkViolation(
            "The offline test suite attempted a real TCP connection to "
            f"{address!r}. Inject a fake fetch/sender instead."
        )
    return _real_create_connection(address, *args, **kwargs)


def _sealed_http_verb(verb: str):
    def _blocked(*args, **kwargs):
        url = args[0] if args else kwargs.get("url")
        label = f"requests.{verb}({url!r})"
        _RECORDED.append(label)
        raise OfflineNetworkViolation(
            f"The offline test suite attempted {label}. "
            "Inject a fake sender / provider instead."
        )

    return _blocked


_PROVIDER_SEAL = _record("sources.base.default_sender (SerpApi/Tavily/Firecrawl)")
_REQUESTS_SESSION_SEAL = _record("requests.Session")
_REQUESTS_GET_SEAL = _sealed_http_verb("get")
_REQUESTS_POST_SEAL = _sealed_http_verb("post")
_REQUESTS_REQUEST_SEAL = _sealed_http_verb("request")
_SMTP_SEAL = _record("smtplib.SMTP")
_SMTP_SSL_SEAL = _record("smtplib.SMTP_SSL")

provider_base.default_sender = _PROVIDER_SEAL
requests.Session = _REQUESTS_SESSION_SEAL
requests.get = _REQUESTS_GET_SEAL
requests.post = _REQUESTS_POST_SEAL
requests.request = _REQUESTS_REQUEST_SEAL
smtplib.SMTP = _SMTP_SEAL
smtplib.SMTP_SSL = _SMTP_SSL_SEAL
socket.getaddrinfo = _sealed_getaddrinfo
socket.create_connection = _sealed_create_connection

try:  # optional dependency: sealed only when importable
    import openai
except ImportError:  # pragma: no cover
    _OPENAI_SEAL = None
else:
    _OPENAI_SEAL = _record("openai.OpenAI")
    openai.OpenAI = _OPENAI_SEAL


class TestSuiteStaysOffline(unittest.TestCase):
    def test_dns_and_connect_seals_block_non_loopback_but_allow_loopback(self):
        # The loopback must stay open: the Stage D web suite binds a real server on
        # 127.0.0.1/test-harness connects to it. Remote hosts must be refused.
        self.assertTrue(_is_loopback("127.0.0.1"))
        self.assertTrue(_is_loopback("localhost"))
        self.assertTrue(_is_loopback(None))
        self.assertFalse(_is_loopback("pbtech.co.nz"))
        self.assertFalse(_is_loopback("1.2.3.4"))
        self.assertTrue(_is_loopback("::1"))

        before = len(_RECORDED)
        try:
            with self.assertRaises(OfflineNetworkViolation):
                socket.getaddrinfo("example.com", 443)
            with self.assertRaises(OfflineNetworkViolation):
                socket.create_connection(("1.2.3.4", 443))
        finally:
            # A deliberate probe must not be mistaken for a suite leak.
            del _RECORDED[before:]

    def test_no_real_network_api_or_smtp_call_was_made_by_the_suite(self):
        self.assertEqual(
            _RECORDED,
            [],
            "Offline tests reached a real outbound boundary: " + repr(_RECORDED),
        )

    def test_the_seals_are_still_armed(self):
        # If a test leaked a patch and left a real function installed, the record
        # above would be blind — so assert the seals themselves are intact.
        self.assertIs(provider_base.default_sender, _PROVIDER_SEAL)
        self.assertIs(requests.Session, _REQUESTS_SESSION_SEAL)
        self.assertIs(requests.get, _REQUESTS_GET_SEAL)
        self.assertIs(requests.post, _REQUESTS_POST_SEAL)
        self.assertIs(requests.request, _REQUESTS_REQUEST_SEAL)
        self.assertIs(smtplib.SMTP, _SMTP_SEAL)
        self.assertIs(smtplib.SMTP_SSL, _SMTP_SSL_SEAL)
        self.assertIs(socket.getaddrinfo, _sealed_getaddrinfo)
        self.assertIs(socket.create_connection, _sealed_create_connection)
        if _OPENAI_SEAL is not None:
            import openai

            self.assertIs(openai.OpenAI, _OPENAI_SEAL)


if __name__ == "__main__":
    unittest.main()
