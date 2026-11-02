"""
auth.py — FastAPI dependency for Firebase Bearer token verification.

Every protected endpoint declares `current_user = Depends(get_current_user)`.
The dependency:
  1. Reads the Authorization: Bearer <token> header
  2. Verifies the token with Firebase Admin
  3. Returns a simple dict with uid + email

Returns HTTP 401 on any auth failure.
No user-facing error message is exposed (server-rules §6.2).
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.core.errors import ErrorCode, raise_api_error
from app.core.firebase import verify_firebase_token

_bearer = HTTPBearer()
_optional_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """Verify Firebase ID token. Returns {uid, email, token} or raises HTTP 401."""
    try:
        decoded = verify_firebase_token(credentials.credentials)
        return {
            "uid": decoded["uid"],
            "email": decoded.get("email", ""),
            "token": decoded,
        }
    except Exception:
        raise_api_error(401, ErrorCode.INVALID_TOKEN)


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> dict | None:
    """Same as get_current_user, but missing or invalid tokens become anonymous."""
    if credentials is None:
        return None
    try:
        decoded = verify_firebase_token(credentials.credentials)
        return {
            "uid": decoded["uid"],
            "email": decoded.get("email", ""),
            "token": decoded,
        }
    except Exception:
        return None
