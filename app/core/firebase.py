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


_BEGIN_PEM = "-----BEGIN PRIVATE KEY-----"
_END_PEM = "-----END PRIVATE KEY-----"


def normalize_private_key(raw: str) -> str:
    """Parse PEM private key from .env / Cloud Run (escaped or real newlines)."""
    if not raw or not raw.strip():
        return ""

    key = raw.strip().lstrip("\ufeff")
    if (key.startswith('"') and key.endswith('"')) or (key.startswith("'") and key.endswith("'")):
        key = key[1:-1].strip()

    # Unescape literal \n (Secret Manager may double-escape as \\n).
    while "\\n" in key:
        key = key.replace("\\n", "\n")
    key = key.replace("\r\n", "\n").replace("\r", "\n")

    # If extra text before/after PEM, extract the block.
    if not key.startswith(_BEGIN_PEM) and _BEGIN_PEM in key:
        start = key.index(_BEGIN_PEM)
        end = key.index(_END_PEM) + len(_END_PEM) if _END_PEM in key else len(key)
        key = key[start:end]

    return key.strip()


def resolve_firebase_private_key() -> str:
    """Return PEM private key from FIREBASE_PRIVATE_KEY or FIREBASE_PRIVATE_KEY_BASE64."""
    if settings.FIREBASE_PRIVATE_KEY_BASE64.strip():
        return normalize_private_key(
            base64.b64decode(settings.FIREBASE_PRIVATE_KEY_BASE64.strip()).decode("utf-8")
        )
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
    if not private_key:
        raise RuntimeError(
            "FIREBASE_PRIVATE_KEY is empty — check the secret is mounted on Cloud Run"
        )
    if not private_key.startswith(_BEGIN_PEM):
        logger.error(
            "FIREBASE_PRIVATE_KEY parse failed (length=%d, prefix=%r). "
            "Secret must start with -----BEGIN PRIVATE KEY-----",
            len(private_key),
            private_key[:30],
        )
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
        cred = credentials.Certificate(build_firebase_service_account_info())
        _app = firebase_admin.initialize_app(cred)
        logger.info("Firebase Admin initialized for project %s", settings.FIREBASE_PROJECT_ID)
    except Exception as exc:
        # Never crash Cloud Run startup — keep /health and send-otp available.
        logger.error(
            "Firebase Admin failed to initialize (%s). "
            "Fix FIREBASE_PRIVATE_KEY (or FIREBASE_PRIVATE_KEY_BASE64) and redeploy. "
            "/auth/verify-otp will return errors until this is resolved.",
            exc,
        )


def verify_firebase_token(token: str) -> dict:
    """
    Verify a Firebase ID token.
    Returns the decoded token dict (contains uid, email, etc.).
    Raises firebase_admin.auth.InvalidIdTokenError on failure.
    """
    return firebase_auth.verify_id_token(token)
