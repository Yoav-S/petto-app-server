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
from app.core.firebase import verify_firebase_token

_bearer = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """Verify Firebase ID token. Returns {uid, email} or raises HTTP 401."""
    try:
        decoded = verify_firebase_token(credentials.credentials)
        return {"uid": decoded["uid"], "email": decoded.get("email", "")}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
