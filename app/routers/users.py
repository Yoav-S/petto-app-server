"""
users.py — /users/me endpoints.

POST /users/me  — upsert user on first login (idempotent)
GET  /users/me  — return current user profile

The upsert pattern ensures that repeated calls (e.g. after reinstall)
never create duplicate user records. The Firebase UID is the stable key.
"""
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from datetime import datetime, timezone

from app.core.database import get_database
from app.core.utils import doc_to_dict
from app.middleware.auth import get_current_user
from app.models.user import UserOut

router = APIRouter(prefix="/users", tags=["users"])


@router.post("/me", response_model=UserOut, status_code=200)
async def upsert_user(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Called after successful Firebase login.
    Creates the user document if it does not exist yet; no-ops if it does.
    Returns the user record in either case (idempotent).
    """
    uid = current_user["uid"]
    email = current_user["email"]

    existing = await db.users.find_one({"firebase_uid": uid})
    if existing:
        return UserOut(**doc_to_dict(existing))

    doc = {
        "firebase_uid": uid,
        "email": email,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.users.insert_one(doc)
    doc["_id"] = result.inserted_id
    return UserOut(**doc_to_dict(doc))


@router.get("/me", response_model=UserOut)
async def get_me(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Return the current user's profile."""
    user = await db.users.find_one({"firebase_uid": current_user["uid"]})
    if not user:
        raise HTTPException(status_code=404, detail="Not found")
    return UserOut(**doc_to_dict(user))
