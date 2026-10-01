"""
firebase.py — Firebase Admin SDK initialization and token verification.

Firebase Admin is initialized once at startup using environment variables.
No service account JSON file is used — credentials are injected via env.
"""
from __future__ import annotations

import base64
import logging
import uuid
from urllib.parse import quote, unquote, urlparse

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
        # Prefer a fully-valid SA (can mint Google tokens). If the key can only
        # verify ID tokens (common with a stale local .env copy), still init so
        # Bearer auth works for local API development.
        try:
            assert_firebase_credentials_valid()
        except Exception as cred_exc:
            logger.warning(
                "Firebase SA refresh failed (%s); initializing for ID-token "
                "verify only (OTP mint / admin APIs may fail).",
                cred_exc,
            )
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


def upload_business_image(business_id: str, content: bytes, content_type: str) -> str:
    """
    Store a listing photo at businesses/{businessId}/{file}.

    Returns a Firebase download URL. The Admin SDK writes this path; browser
    uploads stay under users/{uid}/ and cannot land here.
    """
    from firebase_admin import storage

    extensions = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    extension = extensions.get(content_type)
    bucket_name = settings.firebase_storage_bucket
    if not extension or not bucket_name or _app is None:
        raise RuntimeError("Business image storage is not available")
    token = uuid.uuid4().hex
    name = f"businesses/{business_id}/{uuid.uuid4().hex}{extension}"
    bucket = storage.bucket(bucket_name, app=_app)
    blob = bucket.blob(name)
    blob.metadata = {"firebaseStorageDownloadTokens": token}
    blob.upload_from_string(content, content_type=content_type)
    quoted = quote(name, safe="")
    return (
        f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/{quoted}"
        f"?alt=media&token={token}"
    )


def _business_object_name(business_id: str, url: str) -> str | None:
    parsed = urlparse(url)
    marker = "/o/"
    if marker not in parsed.path:
        return None
    name = unquote(parsed.path.split(marker, 1)[1])
    prefix = f"businesses/{business_id}/"
    if not name.startswith(prefix) or name == prefix:
        return None
    return name


def delete_business_image(business_id: str, url: str) -> None:
    """Delete one listing photo. Ignores URLs that are not in this business folder."""
    from firebase_admin import storage

    name = _business_object_name(business_id, url)
    bucket_name = settings.firebase_storage_bucket
    if not name or not bucket_name or _app is None:
        return
    bucket = storage.bucket(bucket_name, app=_app)
    bucket.blob(name).delete()


def delete_business_images(business_id: str) -> None:
    """Best-effort removal of every photo stored for a business."""
    bucket_name = settings.firebase_storage_bucket
    if not bucket_name or _app is None:
        return
    try:
        from firebase_admin import storage

        bucket = storage.bucket(bucket_name, app=_app)
        for blob in bucket.list_blobs(prefix=f"businesses/{business_id}/"):
            try:
                blob.delete()
            except Exception as exc:  # noqa: BLE001 — per-file best effort
                logger.warning("Failed deleting storage blob %s: %s", blob.name, exc)
    except Exception as exc:  # noqa: BLE001 — cleanup must not block business deletion
        logger.error("Business image cleanup failed for %s: %s", business_id, exc)


def delete_auth_user(uid: str) -> None:
    """
    Delete the Firebase Authentication user.
    Raises firebase_admin.auth.UserNotFoundError if already gone (caller may ignore).
    """
    firebase_auth.delete_user(uid)


def delete_user_storage_files(uid: str) -> int:
    """
    Best-effort deletion of every Storage object under users/{uid}/.

    Returns the number of deleted objects. Never raises — storage cleanup must
    not block account deletion; failures are logged for manual follow-up.
    """
    bucket_name = settings.firebase_storage_bucket
    if not bucket_name:
        logger.warning("Storage cleanup skipped for uid=%s: no bucket configured", uid)
        return 0

    try:
        from firebase_admin import storage

        bucket = storage.bucket(bucket_name, app=_app)
        blobs = list(bucket.list_blobs(prefix=f"users/{uid}/"))
        for blob in blobs:
            try:
                blob.delete()
            except Exception as exc:  # noqa: BLE001 — per-file best effort
                logger.warning("Failed deleting storage blob %s: %s", blob.name, exc)
        return len(blobs)
    except Exception as exc:  # noqa: BLE001 — cleanup is best effort
        logger.error("Storage cleanup failed for uid=%s: %s", uid, exc)
        return 0
