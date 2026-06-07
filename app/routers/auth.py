"""
auth.py — Public auth endpoints (no Bearer token required).

POST /auth/register     — start email signup, send 4-digit OTP
POST /auth/verify-otp   — confirm OTP, create Firebase user, activate MongoDB user
POST /auth/resend-otp   — resend OTP for pending signup
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from firebase_admin import auth as firebase_auth
from firebase_admin.auth import EmailAlreadyExistsError
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_database
from app.core.email_service import send_otp_email
from app.core.otp import (
    OTP_MAX_ATTEMPTS,
    generate_otp_code,
    hash_otp,
    otp_expires_at,
    verify_otp_code,
)
from app.core.security import hash_password
from app.models.auth import (
    AuthMessageResponse,
    RegisterRequest,
    ResendOtpRequest,
    VerifyOtpRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=AuthMessageResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Start email/password signup.
    Stores a pending user + bcrypt password hash, emails a 4-digit OTP.
    """
    email = body.email.lower().strip()

    existing = await db.users.find_one({"email": email})
    if existing and existing.get("email_verified"):
        raise HTTPException(status_code=409, detail="Email already registered")

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
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )

    password_hash = hash_password(body.password)
    await db.users.update_one(
        {"email": email},
        {
            "$set": {
                "email": email,
                "password_hash": password_hash,
                "auth_provider": "email",
                "email_verified": False,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )

    send_otp_email(email, otp_code)
    return AuthMessageResponse(message="Verification code sent to your email")


@router.post("/verify-otp", response_model=AuthMessageResponse)
async def verify_otp(
    body: VerifyOtpRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Verify 4-digit OTP, create Firebase user, mark MongoDB user as verified.
    Client should then sign in with Firebase email/password.
    """
    email = body.email.lower().strip()

    user = await db.users.find_one({"email": email})
    if not user:
        raise HTTPException(status_code=404, detail="Not found")
    if user.get("email_verified"):
        return AuthMessageResponse(message="Email already verified")

    otp_doc = await db.email_otps.find_one({"email": email})
    if not otp_doc:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    if otp_doc.get("attempts", 0) >= OTP_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many attempts")

    expires_at = otp_doc.get("expires_at")
    if expires_at:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="Invalid or expired code")

    if not verify_otp_code(body.otp, otp_doc["otp_hash"]):
        await db.email_otps.update_one(
            {"email": email},
            {"$inc": {"attempts": 1}},
        )
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    # Create Firebase user (or link if already exists from a partial attempt)
    firebase_uid = user.get("firebase_uid")
    try:
        if firebase_uid:
            firebase_auth.update_user(firebase_uid, email_verified=True)
        else:
            fb_user = firebase_auth.create_user(
                email=email,
                password=body.password,
                email_verified=True,
            )
            firebase_uid = fb_user.uid
    except EmailAlreadyExistsError:
        fb_user = firebase_auth.get_user_by_email(email)
        firebase_auth.update_user(fb_user.uid, email_verified=True, password=body.password)
        firebase_uid = fb_user.uid

    now = datetime.now(timezone.utc)
    password_hash = hash_password(body.password)

    await db.users.update_one(
        {"email": email},
        {
            "$set": {
                "firebase_uid": firebase_uid,
                "password_hash": password_hash,
                "auth_provider": "email",
                "email_verified": True,
                "updated_at": now,
            },
        },
    )
    await db.email_otps.delete_one({"email": email})

    return AuthMessageResponse(message="Email verified. You can sign in now.")


@router.post("/resend-otp", response_model=AuthMessageResponse)
async def resend_otp(
    body: ResendOtpRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Resend a fresh 4-digit OTP for a pending signup."""
    email = body.email.lower().strip()

    user = await db.users.find_one({"email": email})
    if not user or user.get("email_verified"):
        # Do not reveal whether email exists
        return AuthMessageResponse(message="If the email is registered, a new code was sent")

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
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )

    send_otp_email(email, otp_code)
    return AuthMessageResponse(message="If the email is registered, a new code was sent")
