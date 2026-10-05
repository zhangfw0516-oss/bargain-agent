"""Tests for notifier.send_email_notification and notify_deal."""

import smtplib
import ssl
import unittest
from unittest.mock import MagicMock, patch

import notifier


class _ServerMock:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.login = MagicMock()
        self.send_message = MagicMock(side_effect=self._maybe_raise)
        self.starttls = MagicMock()
        self.quit = MagicMock()
        self.close = MagicMock()

    def _maybe_raise(self, *args, **kwargs):
        if self.fail:
            raise self.fail

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _server_mock(fail: Exception | None = None):
    return _ServerMock(fail)


class TestNotifier(unittest.TestCase):
    def setUp(self):
        self.config_patch = patch.multiple(
            notifier.config,
            smtp_host=lambda: "smtp.example.com",
            smtp_port=lambda: 465,
            smtp_user=lambda: "user@example.com",
            smtp_password=lambda: "secret",
            smtp_from=lambda: "user@example.com",
            smtp_timeout=lambda: 5.0,
            notification_max_retries=lambda: 2,
        )
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()

    @patch("notifier.smtplib.SMTP_SSL", side_effect=lambda *args, **kwargs: _server_mock())
    def test_send_success(self, mock_ssl):
        result = notifier.send_email_notification("to@example.com", "Subject", "Body")
        self.assertTrue(result["success"])
        self.assertEqual(result["attempts"], 1)
        self.assertIsNone(result["error"])
        self.assertFalse(result["retryable"])

    def test_invalid_email(self):
        with self.assertRaises(notifier.NotifierError):
            notifier.send_email_notification("not-an-email", "Subject", "Body")

    # R11: permanent errors (auth) should not retry.
    @patch("notifier.smtplib.SMTP_SSL", side_effect=lambda *args, **kwargs: _server_mock(fail=smtplib.SMTPAuthenticationError(535, "Auth failed")))
    def test_send_auth_failure_no_retry(self, mock_ssl):
        result = notifier.send_email_notification("to@example.com", "Subject", "Body", max_retries=2)
        self.assertFalse(result["success"])
        self.assertEqual(result["attempts"], 1)
        self.assertIsNotNone(result["error"])
        self.assertFalse(result["retryable"])

    # R11: temporary errors should retry up to budget.
    @patch("notifier.smtplib.SMTP_SSL", side_effect=lambda *args, **kwargs: _server_mock(fail=ConnectionError("refused")))
    def test_send_retryable_failure_retries(self, mock_ssl):
        result = notifier.send_email_notification("to@example.com", "Subject", "Body", max_retries=2)
        self.assertFalse(result["success"])
        self.assertEqual(result["attempts"], 3)
        self.assertTrue(result["retryable"])

    # R11: STARTTLS failures close the connection.
    @patch("notifier.smtplib.SMTP")
    def test_starttls_failure_closes_connection(self, mock_smtp_cls):
        with patch.object(notifier.config, "smtp_port", return_value=587):
            smtp_instance = MagicMock()
            smtp_instance.starttls.side_effect = ssl.SSLError("TLS failed")
            mock_smtp_cls.return_value = smtp_instance

            result = notifier.send_email_notification("to@example.com", "Subject", "Body")
            self.assertFalse(result["success"])
            self.assertEqual(result["attempts"], 1)
            smtp_instance.close.assert_called()

    # R2/R11: notify_deal makes a single attempt so the scheduler owns retries.
    @patch("notifier.send_email_notification", return_value={"success": True, "attempts": 1, "error": None, "retryable": False})
    def test_notify_deal(self, mock_send):
        task = {
            "product_name": "Kindle",
            "target_price": "300.00",
            "currency": "NZD",
            "comparison": "lt",
        }
        quote = {
            "product_name": "Kindle Paperwhite",
            "price": "299.00",
            "currency": "NZD",
            "url": "https://pbtech.co.nz/kindle",
            "retailer": "pbtech.co.nz",
            "in_stock": True,
            "fetched_at": "2026-09-23T04:00:00+00:00",
        }
        result = notifier.notify_deal("to@example.com", task, quote)
        self.assertTrue(result["success"])
        args = mock_send.call_args
        self.assertEqual(args.args[0], "to@example.com")
        self.assertIn("Kindle", args.args[1])
        self.assertEqual(args.kwargs["max_retries"], 0)

    # ---------------------------------------------------------------- F7

    def _smtp_ssl_raising(self, exc, *, on: str = "send"):
        """Patch SMTP_SSL so the given exception is raised during login or send."""

        def factory(*args, **kwargs):
            server = _server_mock()
            if on == "login":
                server.login.side_effect = exc
            else:
                server.send_message.side_effect = exc
            return server

        return patch("notifier.smtplib.SMTP_SSL", side_effect=factory)

    # F7: a 454 temporary authentication pause is retryable, not permanent.
    def test_temporary_auth_code_is_retryable(self):
        exc = smtplib.SMTPAuthenticationError(454, b"Temporary authentication failure")
        with self._smtp_ssl_raising(exc, on="login"):
            result = notifier.send_email_notification("to@example.com", "Subject", "Body", max_retries=2)
        self.assertFalse(result["success"])
        self.assertTrue(result["retryable"])
        self.assertEqual(result["attempts"], 3)

    # F7: a 450 sender deferral is retryable.
    def test_temporary_sender_deferral_is_retryable(self):
        exc = smtplib.SMTPSenderRefused(450, b"try later", "from@example.com")
        with self._smtp_ssl_raising(exc):
            result = notifier.send_email_notification("to@example.com", "Subject", "Body", max_retries=1)
        self.assertTrue(result["retryable"])
        self.assertEqual(result["attempts"], 2)

    # F7: a 550 sender refusal is permanent.
    def test_permanent_sender_refusal_is_not_retryable(self):
        exc = smtplib.SMTPSenderRefused(550, b"not allowed", "from@example.com")
        with self._smtp_ssl_raising(exc):
            result = notifier.send_email_notification("to@example.com", "Subject", "Body", max_retries=2)
        self.assertFalse(result["retryable"])
        self.assertEqual(result["attempts"], 1)

    # F7: RecipientsRefused is judged per recipient code (450 -> retryable).
    def test_temporary_recipient_refusal_is_retryable(self):
        exc = smtplib.SMTPRecipientsRefused({"to@example.com": (450, b"mailbox busy")})
        with self._smtp_ssl_raising(exc):
            result = notifier.send_email_notification("to@example.com", "Subject", "Body", max_retries=1)
        self.assertTrue(result["retryable"])
        self.assertEqual(result["attempts"], 2)

    # F7: RecipientsRefused with only 5xx codes is permanent.
    def test_permanent_recipient_refusal_is_not_retryable(self):
        exc = smtplib.SMTPRecipientsRefused({"to@example.com": (550, b"no such user")})
        with self._smtp_ssl_raising(exc):
            result = notifier.send_email_notification("to@example.com", "Subject", "Body", max_retries=2)
        self.assertFalse(result["retryable"])
        self.assertEqual(result["attempts"], 1)


class TestDealSubjectAndHeadline(unittest.TestCase):
    TASK = {"product_name": "Fitbit Charge 6 Fitness Tracker", "target_price": "218.00", "currency": "NZD", "comparison": "lte"}
    QUOTE = {"product_name": "Fitbit Charge 6", "price": "198.00", "currency": "NZD", "retailer": "PB Tech", "url": "u",
             "in_stock": True, "fetched_at": "t"}

    def test_subject_is_distinctive(self):
        from notifier import deal_subject

        self.assertEqual(deal_subject(self.TASK, self.QUOTE),
                         "[Bargain Agent] 降价提醒：Fitbit Charge 6 Fitness Tracker — NZD 198.00 @ PB Tech")
        long = dict(self.TASK, product_name="X" * 100)
        self.assertLessEqual(len(deal_subject(long, self.QUOTE).split("：")[1].split(" — ")[0]), 60)

    def test_body_starts_with_the_conclusion(self):
        from notifier import build_deal_body

        first = build_deal_body(self.TASK, self.QUOTE).splitlines()[0]
        self.assertIn("PB Tech", first)
        self.assertIn("NZD 198.00", first)
        self.assertIn("218.00", first)


if __name__ == "__main__":
    unittest.main()
