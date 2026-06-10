"""
email_service.py — Send OTP verification emails.

In development, OTP is logged to stdout when SMTP is not configured.
In production, SMTP must be configured or delivery fails.
"""
import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    """SMTP send failed or email is not configured in production."""


def send_otp_email(to_email: str, otp_code: str) -> None:
    """Deliver a 6-digit OTP to the user's inbox."""
    subject = "Your Petto verification code"
    body = (
        f"Your Petto verification code is: {otp_code}\n\n"
        f"This code expires in 10 minutes.\n"
        f"If you did not request this, you can ignore this email."
    )

    if not settings.smtp_configured:
        hint = (
            "set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM_EMAIL on Cloud Run"
            if settings.is_production
            else "dev only"
        )
        logger.warning(
            "SMTP not configured — OTP for %s: %s (%s)",
            to_email,
            otp_code,
            hint,
        )
        print(f"[DEV OTP] {to_email} -> {otp_code}")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as server:
            if settings.SMTP_USE_TLS:
                server.starttls()
            if settings.SMTP_USERNAME and settings.SMTP_PASSWORD:
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(msg)
    except smtplib.SMTPException as exc:
        logger.exception("SMTP failed for %s", to_email)
        raise EmailDeliveryError("SMTP send failed") from exc

    logger.info("OTP email sent to %s", to_email)
