"""
errors.py — Stable API error codes for client-side i18n.

Responses use: {"detail": {"code": "<error_code>"}}
The client maps codes to localized strings (en/he).
"""
from enum import StrEnum

from fastapi import HTTPException


class ErrorCode(StrEnum):
    GENERIC = "generic"
    CHECK_CONNECTION = "check_connection"
    FAILED_TO_SAVE = "failed_to_save"
    NOT_FOUND = "not_found"
    UNAUTHORIZED = "unauthorized"
    INVALID_TOKEN = "invalid_token"
    OTP_INVALID = "otp_invalid"
    OTP_EXPIRED = "otp_expired"
    OTP_TOO_MANY_ATTEMPTS = "otp_too_many_attempts"
    OTP_RESEND_COOLDOWN = "otp_resend_cooldown"
    EMAIL_NOT_VERIFIED = "email_not_verified"
    EMAIL_SEND_FAILED = "email_send_failed"
    NO_FIELDS_TO_UPDATE = "no_fields_to_update"
    ALREADY_RESOLVED = "already_resolved"


def raise_api_error(
    status_code: int,
    code: ErrorCode,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={"code": code.value},
        headers=headers,
    )
