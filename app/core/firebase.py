"""
firebase.py — Firebase Admin SDK initialization and token verification.

Firebase Admin is initialized once at startup using environment variables.
No service account JSON file is used — credentials are injected via env.
"""
from __future__ import annotations

import base64
import logging

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from app.core.config import settings

logger = logging.getLogger(__name__)

_app: firebase_admin.App | None = None

_FIREBASE_SCOPES = [
    "https://www.googleapis.com/auth/firebase",
    "https://www.googleapis.com/auth/cloud-platform",
]


def normalize_private_key(raw: str) -> str:
    """Parse PEM private key from .env / Cloud Run (escaped or quoted newlines)."""
    key = raw.strip()
    if (key.startswith('"') and key.endswith('"')) or (key.startswith("'") and key.endswith("'")):
        key = key[1:-1]
    key = key.replace("\\n", "\n")
    return key


def resolve_firebase_private_key() -> str:
    """Return PEM private key from FIREBASE_PRIVATE_KEY or FIREBASE_PRIVATE_KEY_BASE64."""
    if settings.FIREBASE_PRIVATE_KEY_BASE64.strip():
        return base64.b64decode(settings.FIREBASE_PRIVATE_KEY_BASE64.strip()).decode("utf-8")
    return normalize_private_key(settings.FIREBASE_PRIVATE_KEY)


def build_firebase_service_account_info() -> dict:
    return {
        "type": "service_account",
        "project_id": settings.FIREBASE_PROJECT_ID,
        "client_email": settings.FIREBASE_CLIENT_EMAIL,
        "private_key": resolve_firebase_private_key(),
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def assert_firebase_credentials_valid() -> None:
    """
    Fail fast when the service account key cannot mint Google OAuth tokens.
    Common causes: revoked/rotated key, bad copy/paste, wrong client_email.
    """
    info = build_firebase_service_account_info()
    private_key = info["private_key"]
    if not private_key.startswith("-----BEGIN PRIVATE KEY-----"):
        raise RuntimeError("FIREBASE_PRIVATE_KEY is not a valid PEM private key")

    creds = service_account.Credentials.from_service_account_info(info, scopes=_FIREBASE_SCOPES)
    creds.refresh(Request())


def initialize_firebase() -> None:
    """Initialize Firebase Admin exactly once using env-var credentials."""
    global _app
    if _app is not None:
        return

    try:
        assert_firebase_credentials_valid()
    except Exception as exc:
        logger.error(
            "Firebase Admin credentials are invalid (%s). Regenerate a key in "
            "Firebase Console → Project settings → Service accounts → "
            "Generate new private key, then update FIREBASE_PRIVATE_KEY "
            "(or FIREBASE_PRIVATE_KEY_BASE64) on Cloud Run and in .env.",
            exc,
        )
        if settings.is_production:
            raise RuntimeError("Firebase Admin credentials are invalid") from exc
        logger.warning("Continuing in development — /auth/verify-otp will fail until credentials are fixed")

    cred = credentials.Certificate(build_firebase_service_account_info())
    _app = firebase_admin.initialize_app(cred)


def verify_firebase_token(token: str) -> dict:
    """
    Verify a Firebase ID token.
    Returns the decoded token dict (contains uid, email, etc.).
    Raises firebase_admin.auth.InvalidIdTokenError on failure.
    """
    return firebase_auth.verify_id_token(token)
