"""
firebase.py — Firebase Admin SDK initialization and token verification.

Firebase Admin is initialized once at startup using environment variables.
No service account JSON file is used — credentials are injected via env.
"""
import firebase_admin
from firebase_admin import credentials, auth as firebase_auth
from app.core.config import settings

_app: firebase_admin.App | None = None


def initialize_firebase() -> None:
    """Initialize Firebase Admin exactly once using env-var credentials."""
    global _app
    if _app is not None:
        return

    cred = credentials.Certificate({
        "type": "service_account",
        "project_id": settings.FIREBASE_PROJECT_ID,
        "client_email": settings.FIREBASE_CLIENT_EMAIL,
        # Cloud Run / .env stores private key with escaped newlines
        "private_key": settings.FIREBASE_PRIVATE_KEY.replace("\\n", "\n"),
        "token_uri": "https://oauth2.googleapis.com/token",
    })
    _app = firebase_admin.initialize_app(cred)


def verify_firebase_token(token: str) -> dict:
    """
    Verify a Firebase ID token.
    Returns the decoded token dict (contains uid, email, etc.).
    Raises firebase_admin.auth.InvalidIdTokenError on failure.
    """
    return firebase_auth.verify_id_token(token)
