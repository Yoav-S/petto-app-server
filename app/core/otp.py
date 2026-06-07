"""
otp.py — 6-digit OTP generation and verification.

OTP codes are hashed before storage (same pattern as passwords).
"""
import secrets
from datetime import datetime, timedelta, timezone

from app.core.security import hash_password, verify_password

OTP_LENGTH = 6
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_SECONDS = 20


def generate_otp_code() -> str:
    """Return a zero-padded 6-digit numeric OTP."""
    return f"{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}"


def hash_otp(code: str) -> str:
    return hash_password(code)


def verify_otp_code(code: str, otp_hash: str) -> bool:
    return verify_password(code, otp_hash)


def otp_expires_at() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)
