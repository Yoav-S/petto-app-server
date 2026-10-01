"""
Business publish requests.

An owner submits one listing. Ragly admins, identified by RAGLY_ADMIN_EMAILS,
approve or reject it. A new pending request emails every admin.
"""

import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bson import ObjectId
from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.core.config import settings
from app.core.database import get_database
from app.core.email_service import EmailDeliveryError, send_business_review_email
from app.core.errors import ErrorCode, raise_api_error
from app.core.utils import doc_to_dict
from app.middleware.auth import get_current_user
from app.models.business import (
    AdminPublish,
    BusinessOut,
    BusinessReject,
    BusinessSession,
    BusinessSubmit,
    OpeningHours,
)

logger = logging.getLogger("petto")

router = APIRouter(tags=["businesses"])

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _require_admin(current_user: dict) -> str:
    email = (current_user.get("email") or "").strip().lower()
    if not settings.is_ragly_admin(email):
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    return email


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _validate_hours(hours: OpeningHours) -> dict:
    if hours.always_open:
        return {"always_open": True}
    stored: dict = {"always_open": False}
    for day in _DAYS:
        slots = []
        for slot in getattr(hours, day):
            if not _TIME.match(slot.open) or not _TIME.match(slot.close):
                raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
            if slot.open >= slot.close:
                raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
            slots.append({"open": slot.open, "close": slot.close})
        stored[day] = slots
    return stored


def _clean_photo(value: str | None) -> str | None:
    photo = _clean_optional(value)
    if not photo:
        return None
    if "://" in photo:
        if not photo.startswith("https://"):
            raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
        return photo
    if "/" in photo or "\\" in photo or ".." in photo:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    return photo


def _public_fields(body: BusinessSubmit) -> dict:
    try:
        ZoneInfo(body.timezone.strip())
    except ZoneInfoNotFoundError:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    website = _clean_optional(body.website)
    if website and not website.startswith(("http://", "https://")):
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    longitude, latitude = body.location.coordinates
    return {
        "name": body.name.strip(),
        "phone": body.phone,
        "email": _clean_optional(body.email),
        "description": _clean_optional(body.description),
        "category": body.category,
        "city": body.city.strip(),
        "address": body.address.strip(),
        "timezone": body.timezone.strip(),
        "opening_hours": _validate_hours(body.opening_hours),
        "website": website,
        "location": {"type": "Point", "coordinates": [longitude, latitude]},
        "photo": _clean_photo(body.photo),
        "instagram": _clean_optional(body.instagram),
    }


def _phone_list(doc: dict) -> list[str]:
    raw = doc.get("phone", doc.get("phones"))
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _location(doc: dict) -> dict | None:
    location = doc.get("location")
    if isinstance(location, dict) and isinstance(location.get("coordinates"), list):
        coords = location["coordinates"]
        if len(coords) == 2:
            try:
                return {"type": "Point", "coordinates": [float(coords[0]), float(coords[1])]}
            except (TypeError, ValueError):
                return None
    latitude = doc.get("latitude")
    longitude = doc.get("longitude")
    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
        return {"type": "Point", "coordinates": [float(longitude), float(latitude)]}
    return None


def _hours(doc: dict) -> dict:
    raw = doc.get("opening_hours") if isinstance(doc.get("opening_hours"), dict) else {}
    hours: dict = {"always_open": bool(raw.get("always_open", False))}
    for day in _DAYS:
        slots = []
        for slot in raw.get(day) or []:
            if isinstance(slot, dict) and slot.get("open") and slot.get("close"):
                slots.append({"open": str(slot["open"]), "close": str(slot["close"])})
        hours[day] = slots
    return hours


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _to_out(doc: dict) -> BusinessOut:
    data = doc_to_dict(doc)
    owner_uid = _text(data.get("owner_uid"))
    owner_email = _text(data.get("owner_email"))
    photo = _text(data.get("photo")) or _text(data.get("photo_url"))
    return BusinessOut(
        id=data["id"],
        name=_text(data.get("name")) or "",
        phone=_phone_list(doc),
        email=_text(data.get("email")),
        description=_text(data.get("description")),
        category=data.get("category"),
        city=_text(data.get("city")) or "",
        status=data.get("status"),
        address=_text(data.get("address")) or "",
        timezone=_text(data.get("timezone")) or "Europe/Chisinau",
        opening_hours=_hours(doc),
        website=_text(data.get("website")),
        location=_location(doc),
        photo=photo,
        owner_uid=owner_uid,
        owner_email=owner_email,
        owned=bool(owner_uid),
        instagram=_text(data.get("instagram")),
        rejection_reason=_text(data.get("rejection_reason")),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        submitted_at=data.get("submitted_at"),
    )


def _notify_admins(fields: dict, owner_email: str) -> None:
    review_url = settings.BUSINESS_APP_URL.strip().rstrip("/") + "/admin"
    for admin_email in sorted(settings.ragly_admin_emails):
        try:
            send_business_review_email(
                admin_email,
                business_name=fields["name"],
                owner_email=owner_email,
                city=fields["city"],
                category=fields["category"],
                review_url=review_url,
            )
        except EmailDeliveryError:
            logger.exception("Review email failed for %s", admin_email)


@router.get("/businesses/mine", response_model=BusinessSession)
async def my_business(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Session for the website: admin flag, and this user's listing if any."""
    uid = current_user["uid"]
    email = (current_user.get("email") or "").strip().lower()
    doc = await db.businesses.find_one({"owner_uid": uid})
    if doc is None and email:
        waiting = await db.businesses.find_one(
            {"owner_email": email, "owner_uid": {"$exists": False}}
        )
        if waiting:
            now = datetime.now(timezone.utc)
            await db.businesses.update_one(
                {"_id": waiting["_id"]},
                {"$set": {"owner_uid": uid, "updated_at": now}},
            )
            await db.business_members.update_one(
                {"business_id": str(waiting["_id"]), "user_id": uid},
                {
                    "$set": {
                        "business_id": str(waiting["_id"]),
                        "user_id": uid,
                        "email": email,
                        "role": "owner",
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
            waiting["owner_uid"] = uid
            doc = waiting
    return BusinessSession(
        is_ragly_admin=settings.is_ragly_admin(current_user.get("email")),
        business=_to_out(doc) if doc else None,
    )


@router.post("/businesses", response_model=BusinessOut, status_code=201)
async def submit_business(
    body: BusinessSubmit,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Create or resubmit the signed-in user's listing and email the admins."""
    if settings.is_ragly_admin(current_user.get("email")):
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    fields = _public_fields(body)
    uid = current_user["uid"]
    owner_email = (current_user.get("email") or "").strip().lower()
    now = datetime.now(timezone.utc)
    existing = await db.businesses.find_one({"owner_uid": uid})
    if existing and existing.get("status") == "pending_review":
        raise_api_error(400, ErrorCode.BUSINESS_NOT_PENDING)
    if existing and existing.get("status") == "published":
        raise_api_error(400, ErrorCode.BUSINESS_NOT_PENDING)

    payload = {
        **fields,
        "owner_uid": uid,
        "owner_email": owner_email,
        "status": "pending_review",
        "rejection_reason": None,
        "submitted_at": now,
        "updated_at": now,
    }
    if existing:
        await db.businesses.update_one({"_id": existing["_id"]}, {"$set": payload})
        existing.update(payload)
        doc = existing
    else:
        payload["created_at"] = now
        result = await db.businesses.insert_one(payload)
        payload["_id"] = result.inserted_id
        doc = payload

    _notify_admins(fields, owner_email)
    return _to_out(doc)


@router.post("/admin/businesses", response_model=BusinessOut, status_code=201)
async def publish_for_owner(
    body: AdminPublish,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Publish a listing for an owner who asked by phone. No review email."""
    admin_email = _require_admin(current_user)
    fields = _public_fields(body)
    owner_email = body.owner_email.strip().lower()
    user = await db.users.find_one({"email": owner_email})
    owner_uid = user.get("firebase_uid") if user else None
    taken = await db.businesses.find_one({"owner_email": owner_email})
    if taken or (
        owner_uid and await db.businesses.find_one({"owner_uid": owner_uid})
    ):
        raise_api_error(400, ErrorCode.BUSINESS_NOT_PENDING)
    now = datetime.now(timezone.utc)
    payload = {
        **fields,
        "owner_email": owner_email,
        "status": "published",
        "rejection_reason": None,
        "submitted_at": now,
        "reviewed_at": now,
        "reviewed_by": admin_email,
        "created_at": now,
        "updated_at": now,
    }
    if owner_uid:
        payload["owner_uid"] = owner_uid
    result = await db.businesses.insert_one(payload)
    payload["_id"] = result.inserted_id
    if owner_uid:
        await db.business_members.update_one(
            {"business_id": str(result.inserted_id), "user_id": owner_uid},
            {
                "$set": {
                    "business_id": str(result.inserted_id),
                    "user_id": owner_uid,
                    "email": owner_email,
                    "role": "owner",
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
    return _to_out(payload)


@router.patch("/admin/businesses/{business_id}", response_model=BusinessOut)
async def update_business(
    business_id: str,
    body: BusinessSubmit,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Replace the public listing fields. Ownership and status stay as they are."""
    _require_admin(current_user)
    if not ObjectId.is_valid(business_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    doc = await db.businesses.find_one({"_id": ObjectId(business_id)})
    if not doc:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    now = datetime.now(timezone.utc)
    fields = _public_fields(body)
    fields["updated_at"] = now
    await db.businesses.update_one(
        {"_id": doc["_id"]},
        {
            "$set": fields,
            "$unset": {"phones": "", "latitude": "", "longitude": "", "photo_url": ""},
        },
    )
    doc.update(fields)
    doc.pop("phones", None)
    doc.pop("latitude", None)
    doc.pop("longitude", None)
    doc.pop("photo_url", None)
    return _to_out(doc)


@router.get("/admin/businesses", response_model=list[BusinessOut])
async def list_businesses_for_review(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Pending requests first, then the rest. Admins only."""
    _require_admin(current_user)
    docs = await db.businesses.find({}).sort("submitted_at", -1).to_list(None)
    order = {"pending_review": 0, "rejected": 1, "published": 2, "suspended": 3}
    docs.sort(key=lambda doc: (order.get(doc.get("status"), 9),))
    return [_to_out(doc) for doc in docs]


@router.post("/admin/businesses/{business_id}/approve", response_model=BusinessOut)
async def approve_business(
    business_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Publish the listing and make the submitter its Owner."""
    admin_email = _require_admin(current_user)
    if not ObjectId.is_valid(business_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    doc = await db.businesses.find_one({"_id": ObjectId(business_id)})
    if not doc:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    if doc.get("status") != "pending_review":
        raise_api_error(400, ErrorCode.BUSINESS_NOT_PENDING)
    now = datetime.now(timezone.utc)
    await db.businesses.update_one(
        {"_id": doc["_id"]},
        {
            "$set": {
                "status": "published",
                "rejection_reason": None,
                "reviewed_at": now,
                "reviewed_by": admin_email,
                "updated_at": now,
            }
        },
    )
    await db.business_members.update_one(
        {"business_id": str(doc["_id"]), "user_id": doc["owner_uid"]},
        {
            "$set": {
                "business_id": str(doc["_id"]),
                "user_id": doc["owner_uid"],
                "email": doc.get("owner_email"),
                "role": "owner",
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    doc["status"] = "published"
    doc["rejection_reason"] = None
    doc["updated_at"] = now
    return _to_out(doc)


@router.post("/admin/businesses/{business_id}/reject", response_model=BusinessOut)
async def reject_business(
    business_id: str,
    body: BusinessReject,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Keep the listing off the app and store the reason for the owner."""
    admin_email = _require_admin(current_user)
    if not ObjectId.is_valid(business_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    doc = await db.businesses.find_one({"_id": ObjectId(business_id)})
    if not doc:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    if doc.get("status") != "pending_review":
        raise_api_error(400, ErrorCode.BUSINESS_NOT_PENDING)
    now = datetime.now(timezone.utc)
    reason = body.reason.strip()
    await db.businesses.update_one(
        {"_id": doc["_id"]},
        {
            "$set": {
                "status": "rejected",
                "rejection_reason": reason,
                "reviewed_at": now,
                "reviewed_by": admin_email,
                "updated_at": now,
            }
        },
    )
    doc["status"] = "rejected"
    doc["rejection_reason"] = reason
    doc["updated_at"] = now
    return _to_out(doc)
