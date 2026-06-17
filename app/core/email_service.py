"""
email_service.py — Send OTP verification emails.

Delivery order: Resend API → SMTP → log OTP to stdout (dev / until configured).
"""
import logging
import smtplib
from email.message import EmailMessage

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"


class EmailDeliveryError(Exception):
    """Email send failed or email is not configured in production."""


def _otp_email_content(otp_code: str) -> tuple[str, str]:
    subject = "Your Petto verification code"
    body = (
        f"Your Petto verification code is: {otp_code}\n\n"
        f"This code expires in 10 minutes.\n"
        f"If you did not request this, you can ignore this email."
    )
    return subject, body


def _send_via_resend(to_email: str, subject: str, body: str) -> None:
    response = httpx.post(
        _RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {settings.RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": settings.RESEND_FROM_EMAIL,
            "to": [to_email],
            "subject": subject,
            "text": body,
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        logger.error("Resend API error %s: %s", response.status_code, response.text[:500])
        raise EmailDeliveryError("Resend send failed")


def _send_via_smtp(to_email: str, subject: str, body: str) -> None:
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


def send_otp_email(to_email: str, otp_code: str) -> None:
    """Deliver a 6-digit OTP to the user's inbox."""
    subject, body = _otp_email_content(otp_code)

    if settings.resend_configured:
        _send_via_resend(to_email, subject, body)
        logger.info("OTP email sent via Resend to %s", to_email)
        return

    if settings.smtp_configured:
        _send_via_smtp(to_email, subject, body)
        logger.info("OTP email sent via SMTP to %s", to_email)
        return

    hint = (
        "set RESEND_API_KEY + RESEND_FROM_EMAIL, or SMTP_* vars on Cloud Run"
        if settings.is_production
        else "dev only"
    )
    logger.warning(
        "Email not configured — OTP for %s: %s (%s)",
        to_email,
        otp_code,
        hint,
    )
    print(f"[DEV OTP] {to_email} -> {otp_code}")
