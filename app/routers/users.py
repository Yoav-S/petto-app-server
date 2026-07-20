"""
users.py — /users/me endpoints.

POST /users/me  — upsert user on login (idempotent), update last_login_at
GET  /users/me  — return current user profile
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from datetime import datetime, timezone

from firebase_admin.auth import UserNotFoundError

from app.core.database import get_database
from app.core.firebase import delete_auth_user, delete_user_storage_files
from app.core.utils import doc_to_dict
from app.middleware.auth import get_current_user
from app.models.user import UserOut

from app.core.errors import ErrorCode, raise_api_error

logger = logging.getLogger("petto")

router = APIRouter(prefix="/users", tags=["users"])


def _infer_auth_provider(decoded_token: dict) -> str:
    sign_in_provider = (
        decoded_token.get("firebase", {}).get("sign_in_provider")
        or decoded_token.get("sign_in_provider")
    )
    if sign_in_provider == "google.com":
        return "google"
    return "email"


def _user_to_out(doc: dict, has_pets: bool = False) -> UserOut:
    data = doc_to_dict(doc)
    return UserOut(
        id=data["id"],
        email=data["email"],
        auth_provider=data.get("auth_provider", "email"),
        email_verified=data.get("email_verified", False),
        created_at=data["created_at"],
        last_login_at=data.get("last_login_at"),
        has_pets=has_pets,
    )


async def _user_has_pets(uid: str, db: AsyncIOMotorDatabase) -> bool:
    """Return True if the user owns at least one pet (post-login routing signal)."""
    pet = await db.pets.find_one({"user_id": uid}, {"_id": 1})
    return pet is not None


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
    has_pets = await _user_has_pets(uid, db)

    existing = await db.users.find_one({"firebase_uid": uid})
    if existing:
        if existing.get("auth_provider") == "email" and not existing.get("email_verified", False):
            raise_api_error(403, ErrorCode.EMAIL_NOT_VERIFIED)
        await db.users.update_one(
            {"_id": existing["_id"]},
            {"$set": {"last_login_at": now, "updated_at": now}},
        )
        existing["last_login_at"] = now
        return _user_to_out(existing, has_pets)

    # Google (or first-time) handshake — link by email if pending signup exists
    by_email = await db.users.find_one({"email": email}) if email else None
    if by_email:
        email_verified = (
            True
            if auth_provider == "google"
            else by_email.get("email_verified", False)
        )
        if auth_provider == "email" and not email_verified:
            raise_api_error(403, ErrorCode.EMAIL_NOT_VERIFIED)
        await db.users.update_one(
            {"_id": by_email["_id"]},
            {
                "$set": {
                    "firebase_uid": uid,
                    "auth_provider": auth_provider,
                    "email_verified": email_verified,
                    "last_login_at": now,
                    "updated_at": now,
                },
            },
        )
        by_email["firebase_uid"] = uid
        by_email["auth_provider"] = auth_provider
        by_email["last_login_at"] = now
        return _user_to_out(by_email, has_pets)

    doc = {
        "firebase_uid": uid,
        "email": email,
        "auth_provider": auth_provider,
        "email_verified": auth_provider == "google",
        "created_at": now,
        "last_login_at": now,
        "updated_at": now,
    }
    result = await db.users.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _user_to_out(doc, has_pets)


@router.get("/me", response_model=UserOut)
async def get_me(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Return the current user's profile."""
    user = await db.users.find_one({"firebase_uid": current_user["uid"]})
    if not user:
        raise HTTPException(status_code=404, detail={"code": ErrorCode.NOT_FOUND.value})
    has_pets = await _user_has_pets(current_user["uid"], db)
    return _user_to_out(user, has_pets)


@router.delete("/me", status_code=204)
async def delete_me(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Permanently delete the account and ALL associated data.

    Removal order (data first, auth last so a failure never strands the user
    with an un-loginable account and lingering data):
      1. health_notes -> medical_records -> vaccinations -> reminders -> pets
      2. push_tokens, email_otps, user document
      3. Storage objects under users/{uid}/ (best effort)
      4. Firebase Authentication user
    """
    uid = current_user["uid"]
    user = await db.users.find_one({"firebase_uid": uid})
    email = (user.get("email") if user else None) or (current_user.get("email") or "")
    email = email.lower().strip()

    # 1. Cascade all pet-scoped data.
    pet_ids = [str(doc["_id"]) async for doc in db.pets.find({"user_id": uid}, {"_id": 1})]

    medical_record_ids = [
        str(doc["_id"])
        async for doc in db.medical_records.find({"pet_id": {"$in": pet_ids}}, {"_id": 1})
    ] if pet_ids else []

    if medical_record_ids:
        await db.health_notes.delete_many({"medical_record_id": {"$in": medical_record_ids}})
    if pet_ids:
        await db.medical_records.delete_many({"pet_id": {"$in": pet_ids}})
        await db.vaccinations.delete_many({"pet_id": {"$in": pet_ids}})
        await db.reminders.delete_many({"pet_id": {"$in": pet_ids}})
        await db.pets.delete_many({"user_id": uid})

    # 2. User-scoped data.
    await db.push_tokens.delete_many({"user_id": uid})
    if email:
        await db.email_otps.delete_many({"email": email})
    await db.users.delete_one({"firebase_uid": uid})

    # 3. Uploaded files (best effort — never blocks deletion).
    deleted_files = delete_user_storage_files(uid)
    logger.info("Account deletion uid=%s: removed %d storage objects", uid, deleted_files)

    # 4. Firebase Auth identity (last).
    try:
        delete_auth_user(uid)
    except UserNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.error("Auth user deletion failed for uid=%s: %s", uid, exc)
        raise_api_error(500, ErrorCode.GENERIC)

    return None
