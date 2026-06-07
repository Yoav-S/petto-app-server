"""
users.py — /users/me endpoints.

POST /users/me  — upsert user on login (idempotent), update last_login_at
GET  /users/me  — return current user profile
"""
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from datetime import datetime, timezone

from app.core.database import get_database
from app.core.utils import doc_to_dict
from app.middleware.auth import get_current_user
from app.models.user import UserOut

router = APIRouter(prefix="/users", tags=["users"])


def _infer_auth_provider(decoded_token: dict) -> str:
    sign_in_provider = (
        decoded_token.get("firebase", {}).get("sign_in_provider")
        or decoded_token.get("sign_in_provider")
    )
    if sign_in_provider == "google.com":
        return "google"
    return "email"


def _user_to_out(doc: dict) -> UserOut:
    data = doc_to_dict(doc)
    return UserOut(
        id=data["id"],
        email=data["email"],
        auth_provider=data.get("auth_provider", "email"),
        email_verified=data.get("email_verified", False),
        created_at=data["created_at"],
        last_login_at=data.get("last_login_at"),
    )


@router.post("/me", response_model=UserOut, status_code=200)
async def upsert_user(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Called after successful Firebase login (email or Google).
    Creates the user document if missing; always updates last_login_at.
    """
    uid = current_user["uid"]
    email = (current_user.get("email") or "").lower().strip()
    auth_provider = _infer_auth_provider(current_user.get("token", {}))
    now = datetime.now(timezone.utc)

    existing = await db.users.find_one({"firebase_uid": uid})
    if existing:
        await db.users.update_one(
            {"_id": existing["_id"]},
            {"$set": {"last_login_at": now, "updated_at": now}},
        )
        existing["last_login_at"] = now
        return _user_to_out(existing)

    # Google (or first-time) handshake — link by email if pending signup exists
    by_email = await db.users.find_one({"email": email}) if email else None
    if by_email:
        await db.users.update_one(
            {"_id": by_email["_id"]},
            {
                "$set": {
                    "firebase_uid": uid,
                    "auth_provider": auth_provider,
                    "email_verified": True if auth_provider == "google" else by_email.get("email_verified", False),
                    "last_login_at": now,
                    "updated_at": now,
                },
            },
        )
        by_email["firebase_uid"] = uid
        by_email["auth_provider"] = auth_provider
        by_email["last_login_at"] = now
        return _user_to_out(by_email)

    doc = {
        "firebase_uid": uid,
        "email": email,
        "auth_provider": auth_provider,
        "email_verified": auth_provider == "google",
        "password_hash": None,
        "created_at": now,
        "last_login_at": now,
        "updated_at": now,
    }
    result = await db.users.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _user_to_out(doc)


@router.get("/me", response_model=UserOut)
async def get_me(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Return the current user's profile."""
    user = await db.users.find_one({"firebase_uid": current_user["uid"]})
    if not user:
        raise HTTPException(status_code=404, detail="Not found")
    return _user_to_out(user)
