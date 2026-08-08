"""Notifier module — email and SMS notifications.

To be implemented by Member 3 (Tianshuo Gao).
"""


def send_email_notification(to_email: str, subject: str, body: str) -> None:
    """Send an email alert via SMTP.

    Args:
        to_email: Recipient email address.
        subject: Email subject line.
        body: Email body content.
    """
    # TODO: Wire up SMTP with credentials from .env.
    print(f"[NOTIFIER] Email sent to {to_email}")
    print(f"  Subject: {subject}")
    print(f"  Body: {body}")


def send_sms_notification(phone_number: str, message: str) -> None:
    """Send an SMS alert via third-party API.

    Args:
        phone_number: Recipient phone number.
        message: SMS text content.
    """
    # TODO: Integrate SMS API (e.g., Twilio).
    print(f"[NOTIFIER] SMS sent to {phone_number}: {message}")
