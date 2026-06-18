"""
email_service.py — Send OTP verification emails.

Delivery order: Resend API → SMTP → log OTP to stdout (dev / until configured).
"""
import logging
import smtplib
from email.message import EmailMessage

import httpx

from app.core.gcp_secrets import resolve_resend_credentials

logger = logging.getLogger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"


class EmailDeliveryError(Exception):
    """Email send failed or email is not configured in production."""


def _otp_email_content(otp_code: str) -> tuple[str, str, str]:
    subject = "Your Petto verification code"
    text = (
        f"Your Petto verification code is: {otp_code}\n\n"
        f"This code expires in 10 minutes.\n"
        f"If you did not request this, you can ignore this email."
    )
    html = (
        f"<p>Your Petto verification code is:</p>"
        f"<p style=\"font-size:28px;font-weight:bold;letter-spacing:6px;margin:16px 0\">"
        f"{otp_code}</p>"
        f"<p>This code expires in 10 minutes.</p>"
        f"<p>If you did not request this, you can ignore this email.</p>"
    )
    return subject, text, html


def _send_via_resend(
    to_email: str,
    subject: str,
    text: str,
    html: str,
    api_key: str,
    from_email: str,
) -> None:
    response = httpx.post(
        _RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": from_email,
            "to": [to_email],
            "subject": subject,
            "text": text,
            "html": html,
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        logger.error("Resend API error %s: %s", response.status_code, response.text[:500])
        raise EmailDeliveryError("Resend send failed")
    try:
        message_id = response.json().get("id")
    except Exception:
        message_id = None
    if message_id:
        logger.info("Resend message id: %s", message_id)


def _send_via_smtp(to_email: str, subject: str, body: str) -> None:
    from app.core.config import settings

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
    subject, text, html = _otp_email_content(otp_code)

    resend_key, resend_from, _source = resolve_resend_credentials()
    if resend_key and resend_from:
        _send_via_resend(to_email, subject, text, html, resend_key, resend_from)
        logger.info("OTP email sent via Resend to %s", to_email)
        return

    from app.core.config import settings

    if settings.smtp_configured:
        _send_via_smtp(to_email, subject, text)
        logger.info("OTP email sent via SMTP to %s", to_email)
        return

    hint = (
        "add RESEND_API_KEY + RESEND_FROM_EMAIL to Secret Manager (or Cloud Run env)"
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
