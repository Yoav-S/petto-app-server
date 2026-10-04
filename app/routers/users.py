"""
users.py — /users/me endpoints.

POST /users/me  — upsert user on login (idempotent), update last_login_at
GET  /users/me  — return current user profile
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from motor.motor_asyncio import AsyncIOMotorDatabase
from datetime import datetime, timezone

from firebase_admin import auth as firebase_auth
from firebase_admin.auth import EmailAlreadyExistsError, UserNotFoundError

from app.core.database import get_database
from app.core.firebase import delete_auth_user, delete_user_storage_files
from app.core.utils import doc_to_dict
from app.middleware.auth import get_current_user
from app.models.user import (
    EmailChangeConfirm,
    EmailChangeRequest,
    OnboardingProgress,
    UserProfileUpdate,
    UserOut,
    coherent_onboarding,
)
from app.routers.auth import _store_and_send_otp
from app.models.subscription import SubscriptionOut
from app.core.subscription import normalize_subscription

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


def _subscription_out(doc: dict) -> SubscriptionOut:
    return SubscriptionOut(**normalize_subscription(doc.get("subscription")))


def _account_name(doc: dict) -> str | None:
    raw = doc.get("name")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _optional_text(doc: dict, key: str) -> str | None:
    raw = doc.get(key)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _onboarding_out(doc: dict, has_pets: bool) -> OnboardingProgress | None:
    """Finished accounts never resume setup. A broken draft is ignored."""
    if has_pets:
        return None
    raw = doc.get("onboarding")
    if not isinstance(raw, dict):
        return None
    try:
        return coherent_onboarding(OnboardingProgress.model_validate(raw))
    except ValidationError:
        return None


def _user_to_out(doc: dict, has_pets: bool = False) -> UserOut:
    data = doc_to_dict(doc)
    return UserOut(
        id=data["id"],
        email=data["email"],
        name=_account_name(doc),
        phone=_optional_text(doc, "phone"),
        photo_url=_optional_text(doc, "photo_url"),
        auth_provider=data.get("auth_provider", "email"),
        email_verified=data.get("email_verified", False),
        created_at=data["created_at"],
        last_login_at=data.get("last_login_at"),
        has_pets=has_pets,
        onboarding=_onboarding_out(doc, has_pets),
        subscription=_subscription_out(doc),
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
        "subscription": {
            "plan": "free",
            "provider": None,
            "product_id": None,
            "expires_at": None,
            "will_renew": False,
            "updated_at": None,
        },
    }
    result = await db.users.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _user_to_out(doc, has_pets)


@router.patch("/me", response_model=UserOut)
async def update_me(
    body: UserProfileUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Store the account name. Phone and photo update only when the client sends them."""
    uid = current_user["uid"]
    user = await db.users.find_one({"firebase_uid": uid})
    if not user:
        raise HTTPException(status_code=404, detail={"code": ErrorCode.NOT_FOUND.value})
    now = datetime.now(timezone.utc)
    updates: dict = {"name": body.name, "updated_at": now}
    if "phone" in body.model_fields_set:
        updates["phone"] = body.phone
    if "photo_url" in body.model_fields_set:
        updates["photo_url"] = body.photo_url
    await db.users.update_one({"_id": user["_id"]}, {"$set": updates})
    user.update(updates)
    has_pets = await _user_has_pets(uid, db)
    return _user_to_out(user, has_pets)


@router.patch("/me/onboarding", response_model=UserOut)
async def save_onboarding(
    body: OnboardingProgress,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Remember the first-pet step and the answers already given.
    A later login resumes this step. Creating the pet clears it.
    Once a pet exists, the draft is left untouched and not returned.
    """
    uid = current_user["uid"]
    user = await db.users.find_one({"firebase_uid": uid})
    if not user:
        raise HTTPException(status_code=404, detail={"code": ErrorCode.NOT_FOUND.value})
    has_pets = await _user_has_pets(uid, db)
    if has_pets:
        return _user_to_out(user, True)
    progress = coherent_onboarding(body)
    now = datetime.now(timezone.utc)
    stored = progress.model_dump()
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"onboarding": stored, "updated_at": now}},
    )
    user["onboarding"] = stored
    return _user_to_out(user, False)


def _normalize_email(value: str) -> str:
    return value.lower().strip()


async def _email_owned_by_someone_else(
    db: AsyncIOMotorDatabase,
    email: str,
    uid: str,
) -> bool:
    other = await db.users.find_one({"email": email})
    if other and other.get("firebase_uid") != uid:
        return True
    try:
        existing = firebase_auth.get_user_by_email(email)
    except UserNotFoundError:
        return False
    return existing.uid != uid


@router.post("/me/email/otp", status_code=200)
async def send_email_change_otp(
    body: EmailChangeRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Send a code to a new email. Nothing on the account changes until it is confirmed."""
    uid = current_user["uid"]
    user = await db.users.find_one({"firebase_uid": uid})
    if not user:
        raise HTTPException(status_code=404, detail={"code": ErrorCode.NOT_FOUND.value})
    email = _normalize_email(str(body.email))
    current = _normalize_email(user.get("email") or "")
    if email == current:
        raise_api_error(400, ErrorCode.NO_FIELDS_TO_UPDATE)
    if await _email_owned_by_someone_else(db, email, uid):
        raise_api_error(409, ErrorCode.EMAIL_IN_USE)
    await _store_and_send_otp(db, email)
    await db.email_otps.update_one(
        {"email": email},
        {"$set": {"purpose": "email_change", "firebase_uid": uid}},
    )
    return {"message": "verification_sent"}


@router.post("/me/email/confirm", response_model=UserOut)
async def confirm_email_change(
    body: EmailChangeConfirm,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Apply the new email only after the OTP matches. Cancel leaves the old email."""
    from app.core.otp import OTP_MAX_ATTEMPTS, verify_otp_code

    uid = current_user["uid"]
    user = await db.users.find_one({"firebase_uid": uid})
    if not user:
        raise HTTPException(status_code=404, detail={"code": ErrorCode.NOT_FOUND.value})
    email = _normalize_email(str(body.email))
    if await _email_owned_by_someone_else(db, email, uid):
        raise_api_error(409, ErrorCode.EMAIL_IN_USE)
    otp_doc = await db.email_otps.find_one({"email": email})
    if (
        not otp_doc
        or otp_doc.get("purpose") != "email_change"
        or otp_doc.get("firebase_uid") != uid
    ):
        raise_api_error(400, ErrorCode.OTP_INVALID)
    if otp_doc.get("attempts", 0) >= OTP_MAX_ATTEMPTS:
        raise_api_error(429, ErrorCode.OTP_TOO_MANY_ATTEMPTS)
    expires_at = otp_doc.get("expires_at")
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            raise_api_error(400, ErrorCode.OTP_EXPIRED)
    if not verify_otp_code(body.otp, otp_doc["otp_hash"]):
        await db.email_otps.update_one({"email": email}, {"$inc": {"attempts": 1}})
        raise_api_error(400, ErrorCode.OTP_INVALID)
    try:
        firebase_auth.update_user(uid, email=email, email_verified=True)
    except EmailAlreadyExistsError:
        raise_api_error(409, ErrorCode.EMAIL_IN_USE)
    except UserNotFoundError:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    now = datetime.now(timezone.utc)
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"email": email, "email_verified": True, "updated_at": now}},
    )
    await db.email_otps.delete_one({"email": email})
    user["email"] = email
    user["email_verified"] = True
    has_pets = await _user_has_pets(uid, db)
    return _user_to_out(user, has_pets)


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
