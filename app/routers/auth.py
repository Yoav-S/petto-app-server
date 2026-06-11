"""
auth.py — Public passwordless auth endpoints (no Bearer token required).

POST /auth/send-otp    — signup + login: email only, sends 6-digit OTP
POST /auth/verify-otp  — verify OTP, return Firebase custom token
POST /auth/resend-otp  — resend OTP (20s cooldown)
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from firebase_admin import auth as firebase_auth
from firebase_admin.auth import EmailAlreadyExistsError
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_database
from app.core.email_service import EmailDeliveryError, send_otp_email
from app.core.otp import (
    OTP_MAX_ATTEMPTS,
    OTP_RESEND_COOLDOWN_SECONDS,
    generate_otp_code,
    hash_otp,
    otp_expires_at,
    verify_otp_code,
)
from app.models.auth import (
    AuthMessageResponse,
    ResendOtpRequest,
    SendOtpRequest,
    VerifyOtpRequest,
    VerifyOtpResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

_GENERIC_ERROR = "Something went wrong"
_RESEND_MESSAGE = "If this email is valid, a verification code was sent"


async def _store_and_send_otp(db: AsyncIOMotorDatabase, email: str) -> None:
    otp_code = generate_otp_code()
    now = datetime.now(timezone.utc)

    await db.email_otps.update_one(
        {"email": email},
        {
            "$set": {
                "email": email,
                "otp_hash": hash_otp(otp_code),
                "expires_at": otp_expires_at(),
                "attempts": 0,
                "last_sent_at": now,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    try:
        send_otp_email(email, otp_code)
    except EmailDeliveryError:
        raise HTTPException(status_code=500, detail=_GENERIC_ERROR) from None


def _resend_cooldown_remaining(otp_doc: dict | None) -> int | None:
    if not otp_doc or not otp_doc.get("last_sent_at"):
        return None
    last_sent = otp_doc["last_sent_at"]
    if last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - last_sent).total_seconds()
    if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
        return max(1, int(OTP_RESEND_COOLDOWN_SECONDS - elapsed))
    return None


def _raise_resend_cooldown(otp_doc: dict | None) -> None:
    remaining = _resend_cooldown_remaining(otp_doc)
    if remaining is not None:
        raise HTTPException(
            status_code=429,
            detail=_GENERIC_ERROR,
            headers={"Retry-After": str(remaining)},
        )


@router.post("/send-otp", response_model=AuthMessageResponse, status_code=status.HTTP_200_OK)
async def send_otp(
    body: SendOtpRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Passwordless signup/login step 1.
    Works for new and returning users — always sends a 6-digit OTP.
    """
    email = body.email.lower().strip()
    now = datetime.now(timezone.utc)

    try:
        existing_otp = await db.email_otps.find_one({"email": email})
        _raise_resend_cooldown(existing_otp)

        # Upsert pending user — do NOT set firebase_uid: null (unique sparse index
        # only allows one null value and would 500 on the second signup).
        await db.users.update_one(
            {"email": email},
            {
                "$set": {
                    "email": email,
                    "auth_provider": "email",
                    "email_verified": False,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

        await _store_and_send_otp(db, email)
    except HTTPException:
        raise
    except Exception:
        logger.exception("send-otp failed for %s", email)
        raise HTTPException(status_code=500, detail=_GENERIC_ERROR) from None

    return AuthMessageResponse(message=_RESEND_MESSAGE)


@router.post("/verify-otp", response_model=VerifyOtpResponse)
async def verify_otp(
    body: VerifyOtpRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Passwordless step 2.
    Verifies 6-digit OTP, ensures Firebase user exists, returns custom token.
    Client calls signInWithCustomToken(custom_token) — session persists until sign-out.
    """
    email = body.email.lower().strip()

    user = await db.users.find_one({"email": email})
    if not user:
        raise HTTPException(status_code=400, detail=_GENERIC_ERROR)

    otp_doc = await db.email_otps.find_one({"email": email})
    if not otp_doc:
        raise HTTPException(status_code=400, detail=_GENERIC_ERROR)

    if otp_doc.get("attempts", 0) >= OTP_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail=_GENERIC_ERROR)

    expires_at = otp_doc.get("expires_at")
    if expires_at:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail=_GENERIC_ERROR)

    if not verify_otp_code(body.otp, otp_doc["otp_hash"]):
        await db.email_otps.update_one({"email": email}, {"$inc": {"attempts": 1}})
        raise HTTPException(status_code=400, detail=_GENERIC_ERROR)

    try:
        firebase_uid = user.get("firebase_uid")
        try:
            if firebase_uid:
                firebase_auth.update_user(firebase_uid, email_verified=True)
            else:
                fb_user = firebase_auth.create_user(email=email, email_verified=True)
                firebase_uid = fb_user.uid
        except EmailAlreadyExistsError:
            fb_user = firebase_auth.get_user_by_email(email)
            firebase_auth.update_user(fb_user.uid, email_verified=True)
            firebase_uid = fb_user.uid

        token_bytes = firebase_auth.create_custom_token(firebase_uid)
        custom_token = token_bytes.decode("utf-8") if isinstance(token_bytes, bytes) else token_bytes

        now = datetime.now(timezone.utc)
        await db.users.update_one(
            {"email": email},
            {
                "$set": {
                    "firebase_uid": firebase_uid,
                    "auth_provider": "email",
                    "email_verified": True,
                    "last_login_at": now,
                    "updated_at": now,
                },
            },
        )
        await db.email_otps.delete_one({"email": email})
    except HTTPException:
        raise
    except Exception:
        logger.exception("verify-otp failed for %s", email)
        raise HTTPException(status_code=500, detail=_GENERIC_ERROR) from None

    return VerifyOtpResponse(custom_token=custom_token)


@router.post("/resend-otp", response_model=AuthMessageResponse)
async def resend_otp(
    body: ResendOtpRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Resend OTP — enforces 20-second cooldown between sends."""
    email = body.email.lower().strip()

    otp_doc = await db.email_otps.find_one({"email": email})
    if not otp_doc:
        return AuthMessageResponse(message=_RESEND_MESSAGE)

    _raise_resend_cooldown(otp_doc)
    try:
        await _store_and_send_otp(db, email)
    except HTTPException:
        raise
    except Exception:
        logger.exception("resend-otp failed for %s", email)
        raise HTTPException(status_code=500, detail=_GENERIC_ERROR) from None
    return AuthMessageResponse(message=_RESEND_MESSAGE)
