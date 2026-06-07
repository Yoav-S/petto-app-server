"""
email_service.py — Send OTP verification emails.

In development, OTP is logged to stdout when SMTP is not configured.
"""
import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger(__name__)


def send_otp_email(to_email: str, otp_code: str) -> None:
    """Deliver a 4-digit OTP to the user's inbox."""
    subject = "Your Petto verification code"
    body = (
        f"Your Petto verification code is: {otp_code}\n\n"
        f"This code expires in 10 minutes.\n"
        f"If you did not request this, you can ignore this email."
    )

    if not settings.smtp_configured:
        logger.warning(
            "SMTP not configured — OTP for %s: %s (dev only)",
            to_email,
            otp_code,
        )
        print(f"[DEV OTP] {to_email} -> {otp_code}")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as server:
        if settings.SMTP_USE_TLS:
            server.starttls()
        if settings.SMTP_USERNAME and settings.SMTP_PASSWORD:
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.send_message(msg)
