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

# App locales: en / he / ro / ru (matches the client).
_SUPPORTED_LOCALES = frozenset({"en", "he", "ro", "ru"})

_OTP_COPY: dict[str, dict[str, str]] = {
    "en": {
        "subject": "Your Ragly verification code",
        "intro": "Your verification code for the Ragly app is:",
        "expires": "This code expires in 10 minutes.",
        "ignore": "If you did not request this code, you can safely ignore this email.",
    },
    "he": {
        "subject": "קוד האימות שלך ב-Ragly",
        "intro": "קוד האימות שלך לאפליקציית Ragly הוא:",
        "expires": "הקוד בתוקף ל-10 דקות.",
        "ignore": "אם לא ביקשת את הקוד הזה, אפשר להתעלם מהמייל בבטחה.",
    },
    "ro": {
        "subject": "Codul tău de verificare Ragly",
        "intro": "Codul tău de verificare pentru aplicația Ragly este:",
        "expires": "Acest cod expiră în 10 minute.",
        "ignore": "Dacă nu ai solicitat acest cod, poți ignora acest e-mail în siguranță.",
    },
    "ru": {
        "subject": "Ваш код подтверждения Ragly",
        "intro": "Ваш код подтверждения для приложения Ragly:",
        "expires": "Срок действия кода — 10 минут.",
        "ignore": "Если вы не запрашивали этот код, просто проигнорируйте это письмо.",
    },
}


class EmailDeliveryError(Exception):
    """Email send failed or email is not configured in production."""


def normalize_email_locale(locale: str | None) -> str:
    """Map client/device locale to a supported email language (default en)."""
    if not locale:
        return "en"
    code = locale.strip().lower().replace("_", "-")
    primary = code.split("-", 1)[0]
    return primary if primary in _SUPPORTED_LOCALES else "en"


def _otp_email_content(otp_code: str, locale: str | None = None) -> tuple[str, str, str]:
    lang = normalize_email_locale(locale)
    copy = _OTP_COPY[lang]
    rtl = lang == "he"
    dir_attr = ' dir="rtl"' if rtl else ""

    subject = copy["subject"]
    text = (
        f"{copy['intro']} {otp_code}\n\n"
        f"{copy['expires']}\n"
        f"{copy['ignore']}\n"
    )
    html = (
        f"<div{dir_attr} style=\"font-family:Arial,Helvetica,sans-serif;color:#1F2937;"
        f"line-height:1.5;max-width:480px\">"
        f"<p style=\"margin:0 0 8px;font-size:16px\"><strong>Ragly</strong></p>"
        f"<p style=\"margin:0 0 12px\">{copy['intro']}</p>"
        f"<p style=\"font-size:28px;font-weight:bold;letter-spacing:6px;margin:16px 0\">"
        f"{otp_code}</p>"
        f"<p style=\"margin:0 0 8px\">{copy['expires']}</p>"
        f"<p style=\"margin:0;color:#6B7280;font-size:14px\">{copy['ignore']}</p>"
        f"</div>"
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


def send_otp_email(to_email: str, otp_code: str, locale: str | None = None) -> None:
    """Deliver a 6-digit OTP to the user's inbox (localized)."""
    subject, text, html = _otp_email_content(otp_code, locale)

    resend_key, resend_from, _source = resolve_resend_credentials()
    if resend_key and resend_from:
        _send_via_resend(to_email, subject, text, html, resend_key, resend_from)
        logger.info(
            "OTP email sent via Resend to %s (locale=%s)",
            to_email,
            normalize_email_locale(locale),
        )
        return

    from app.core.config import settings

    if settings.smtp_configured:
        _send_via_smtp(to_email, subject, text)
        logger.info(
            "OTP email sent via SMTP to %s (locale=%s)",
            to_email,
            normalize_email_locale(locale),
        )
        return

    hint = (
        "add RESEND_API_KEY + RESEND_FROM_EMAIL to Secret Manager (or Cloud Run env)"
        if settings.is_production
        else "dev only"
    )
    logger.warning(
        "Email not configured — OTP for %s: %s (%s, locale=%s)",
        to_email,
        otp_code,
        hint,
        normalize_email_locale(locale),
    )
    print(f"[DEV OTP] {to_email} -> {otp_code} (locale={normalize_email_locale(locale)})")
