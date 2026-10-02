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
from typing import Literal

from fastapi import APIRouter, Depends, File, Query, UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.core.config import settings
from app.core.database import get_database
from app.core.firebase import delete_business_image, delete_business_images, upload_business_image
from app.core.email_service import (
    EmailDeliveryError,
    send_business_invite_email,
    send_business_owner_email,
    send_business_review_email,
)
from app.core.errors import ErrorCode, raise_api_error
from app.core.utils import doc_to_dict
from app.middleware.auth import get_current_user
from app.models.business import (
    AdminPublish,
    BusinessCounts,
    BusinessOut,
    BusinessPage,
    BusinessReject,
    BusinessSession,
    BusinessSubmit,
    InvitationOut,
    OpeningHours,
    OwnerInvite,
    PhotoRemove,
    TeamInvite,
)

logger = logging.getLogger("petto")

router = APIRouter(tags=["businesses"])

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MAX_PHOTOS = 6
_MAX_PHOTO_BYTES = 5 * 1024 * 1024
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


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
    """Store only the seven days. An empty day is closed. No always_open flag."""
    if hours.always_open:
        return {day: [{"open": "00:00", "close": "23:59"}] for day in _DAYS}
    stored: dict = {}
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
    fields = {
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
    }
    instagram = _clean_optional(body.instagram)
    if instagram:
        fields["instagram"] = instagram
    return fields


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


def _field_errors(doc: dict) -> dict[str, str]:
    raw = doc.get("field_errors")
    if not isinstance(raw, dict):
        return {}
    return {
        key: value.strip()
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }


def _gallery(doc: dict) -> list[str]:
    raw = doc.get("photos")
    if not isinstance(raw, list):
        return []
    return [
        item
        for item in raw
        if isinstance(item, str) and item.startswith("https://")
    ][:_MAX_PHOTOS]


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
    photos = _gallery(doc)
    if photo and photo.startswith("https://") and photo not in photos:
        photos = [photo, *photos][:_MAX_PHOTOS]
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
        photos=photos,
        owner_uid=owner_uid,
        owner_email=owner_email,
        owned=bool(owner_uid),
        instagram=_text(data.get("instagram")),
        rejection_reason=_text(data.get("rejection_reason")),
        field_errors=_field_errors(doc),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        submitted_at=data.get("submitted_at"),
    )


def _invite_out(doc: dict, business_name: str = "") -> InvitationOut:
    data = doc_to_dict(doc)
    return InvitationOut(
        id=data["id"],
        business_id=str(data.get("business_id") or ""),
        business_name=business_name,
        email=_text(data.get("email")) or "",
        role=data.get("role"),
        status=data.get("status"),
    )


async def _load_invitations(db: AsyncIOMotorDatabase, business_ids: list[str]) -> dict[str, list[InvitationOut]]:
    if not business_ids:
        return {}
    rows = await db.business_invitations.find({"business_id": {"$in": business_ids}}).to_list(None)
    grouped: dict[str, list[InvitationOut]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("business_id")), []).append(_invite_out(row))
    return grouped


async def _owner_business_ids(db: AsyncIOMotorDatabase, business_ids: list[str]) -> set[str]:
    if not business_ids:
        return set()
    rows = await db.business_members.find(
        {"business_id": {"$in": business_ids}, "role": "owner"}
    ).to_list(None)
    return {str(row.get("business_id")) for row in rows}


def _apply_team(business: BusinessOut, invitations: list[InvitationOut], owner_ids: set[str]) -> BusinessOut:
    business.invitations = invitations
    if business.id in owner_ids:
        business.owned = True
    return business


def _send_invite(email: str, business_name: str, role: str) -> None:
    accept_url = settings.BUSINESS_APP_URL.strip().rstrip("/") + "/dashboard"
    try:
        send_business_invite_email(
            email,
            business_name=business_name,
            role=role,
            accept_url=accept_url,
        )
    except EmailDeliveryError:
        logger.exception("Invite email failed for %s", email)


async def _create_invitation(
    db: AsyncIOMotorDatabase,
    business: dict,
    email: str,
    role: str,
    invited_by: str,
) -> dict:
    email = email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    business_id = str(business["_id"])
    member = await db.business_members.find_one({"business_id": business_id, "email": email})
    if member:
        raise_api_error(400, ErrorCode.ALREADY_RESOLVED)
    pending = await db.business_invitations.find_one(
        {"business_id": business_id, "email": email, "status": "pending"}
    )
    if pending:
        raise_api_error(400, ErrorCode.ALREADY_RESOLVED)
    now = datetime.now(timezone.utc)
    payload = {
        "business_id": business_id,
        "email": email,
        "role": role,
        "status": "pending",
        "invited_by": invited_by,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.business_invitations.insert_one(payload)
    payload["_id"] = result.inserted_id
    _send_invite(email, business.get("name") or "", role)
    return payload


async def _delete_business(db: AsyncIOMotorDatabase, doc: dict) -> None:
    business_id = str(doc["_id"])
    await db.businesses.delete_one({"_id": doc["_id"]})
    await db.business_members.delete_many({"business_id": business_id})
    await db.business_invitations.delete_many({"business_id": business_id})
    await db.business_deletions.insert_one(
        {"business_id": business_id, "deleted_at": datetime.now(timezone.utc)}
    )
    delete_business_images(business_id)


async def _with_team(db: AsyncIOMotorDatabase, business: BusinessOut) -> BusinessOut:
    invitations = await _load_invitations(db, [business.id])
    owner_ids = await _owner_business_ids(db, [business.id])
    return _apply_team(business, invitations.get(business.id, []), owner_ids)


async def _listing_owner(db: AsyncIOMotorDatabase, business: dict, uid: str) -> bool:
    """The account that submitted the listing, or a member with the owner role."""
    if business.get("owner_uid") == uid:
        return True
    member = await db.business_members.find_one(
        {"business_id": str(business["_id"]), "user_id": uid}
    )
    return bool(member and member.get("role") == "owner")


async def _add_business_photo(
    db: AsyncIOMotorDatabase,
    business: dict,
    file: UploadFile,
) -> BusinessOut:
    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    content = await file.read(_MAX_PHOTO_BYTES + 1)
    if content_type not in _IMAGE_TYPES or not content or len(content) > _MAX_PHOTO_BYTES:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    photos = _gallery(business)
    if len(photos) >= _MAX_PHOTOS:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    try:
        url = upload_business_image(str(business["_id"]), content, content_type)
    except Exception:
        logger.exception("Business photo upload failed for %s", business.get("_id"))
        raise_api_error(500, ErrorCode.FAILED_TO_SAVE)
    photos.append(url)
    now = datetime.now(timezone.utc)
    await db.businesses.update_one(
        {"_id": business["_id"]},
        {"$set": {"photos": photos, "updated_at": now}},
    )
    business["photos"] = photos
    business["updated_at"] = now
    return await _with_team(db, _to_out(business))


async def _remove_business_photo(
    db: AsyncIOMotorDatabase,
    business: dict,
    url: str,
) -> BusinessOut:
    url = url.strip()
    photos = _gallery(business)
    photo = _text(business.get("photo")) or _text(business.get("photo_url"))
    if url not in photos and photo != url:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    photos = [item for item in photos if item != url]
    now = datetime.now(timezone.utc)
    update: dict = {"photos": photos, "updated_at": now}
    unset: dict = {}
    if photo == url:
        unset["photo"] = ""
        business.pop("photo", None)
    await db.businesses.update_one(
        {"_id": business["_id"]},
        {"$set": update, **({"$unset": unset} if unset else {})},
    )
    business["photos"] = photos
    business["updated_at"] = now
    try:
        delete_business_image(str(business["_id"]), url)
    except Exception:
        logger.exception("Business photo delete failed for %s", business.get("_id"))
    return await _with_team(db, _to_out(business))


def _email_owner(
    email: str | None,
    *,
    business_name: str,
    subject: str,
    intro: str,
    lines: list[str] | None = None,
) -> None:
    if not email:
        return
    dashboard = settings.BUSINESS_APP_URL.strip().rstrip("/") + "/dashboard"
    try:
        send_business_owner_email(
            email,
            business_name=business_name,
            subject=subject,
            intro=intro,
            lines=lines,
            action_url=dashboard,
        )
    except EmailDeliveryError:
        logger.exception("Owner email failed for %s", email)


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
    """Session for the website: admin flag, membership, and pending invitations."""
    uid = current_user["uid"]
    email = (current_user.get("email") or "").strip().lower()
    member = await db.business_members.find_one({"user_id": uid})
    role = member.get("role") if member else None
    doc = None
    if member and ObjectId.is_valid(str(member.get("business_id"))):
        doc = await db.businesses.find_one({"_id": ObjectId(member["business_id"])})
    if doc is None:
        doc = await db.businesses.find_one({"owner_uid": uid})
    business = _to_out(doc) if doc else None
    if business:
        invitations = await _load_invitations(db, [business.id])
        owner_ids = await _owner_business_ids(db, [business.id])
        _apply_team(business, invitations.get(business.id, []), owner_ids)
    pending_rows = []
    if email:
        pending_rows = await db.business_invitations.find(
            {"email": email, "status": "pending"}
        ).to_list(None)
    pending: list[InvitationOut] = []
    for row in pending_rows:
        business_name = ""
        if ObjectId.is_valid(str(row.get("business_id"))):
            invited_business = await db.businesses.find_one(
                {"_id": ObjectId(row["business_id"])}
            )
            if invited_business:
                business_name = invited_business.get("name") or ""
        pending.append(_invite_out(row, business_name))
    return BusinessSession(
        is_ragly_admin=settings.is_ragly_admin(current_user.get("email")),
        business=business,
        role=role,
        invitations=pending,
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
        "field_errors": {},
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
    _email_owner(
        owner_email,
        business_name=fields["name"],
        subject=f"Ragly: {fields['name']} is waiting for review",
        intro=f"We received {fields['name']}. It stays pending until a Ragly admin approves it.",
    )
    return _to_out(doc)


@router.post("/admin/businesses", response_model=BusinessOut, status_code=201)
async def publish_for_owner(
    body: AdminPublish,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Publish a listing. An owner email, when present, gets an invitation."""
    admin_email = _require_admin(current_user)
    fields = _public_fields(body)
    owner_email = (body.owner_email or "").strip().lower()
    now = datetime.now(timezone.utc)
    payload = {
        **fields,
        "status": "published",
        "rejection_reason": None,
        "submitted_at": now,
        "reviewed_at": now,
        "reviewed_by": admin_email,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.businesses.insert_one(payload)
    payload["_id"] = result.inserted_id
    business = _to_out(payload)
    if owner_email:
        invite = await _create_invitation(db, payload, owner_email, "owner", admin_email)
        business.invitations = [_invite_out(invite, payload["name"])]
    return business


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
    unset = {"phones": "", "latitude": "", "longitude": "", "photo_url": ""}
    if "instagram" not in fields:
        unset["instagram"] = ""
    await db.businesses.update_one(
        {"_id": doc["_id"]},
        {"$set": fields, "$unset": unset},
    )
    doc.update(fields)
    if "instagram" not in fields:
        doc.pop("instagram", None)
    doc.pop("phones", None)
    doc.pop("latitude", None)
    doc.pop("longitude", None)
    doc.pop("photo_url", None)
    return _to_out(doc)


async def _decorate(db: AsyncIOMotorDatabase, docs: list[dict]) -> list[BusinessOut]:
    businesses = [_to_out(doc) for doc in docs]
    business_ids = [business.id for business in businesses]
    invitations = await _load_invitations(db, business_ids)
    owner_ids = await _owner_business_ids(db, business_ids)
    return [
        _apply_team(business, invitations.get(business.id, []), owner_ids)
        for business in businesses
    ]


def _page_match(
    status: str | None,
    category: str | None,
    q: str | None,
    ownership: str,
    owner_oids: list[ObjectId],
) -> dict:
    clauses: list[dict] = []
    if status == "reviewed":
        clauses.append({"status": {"$in": ["published", "rejected"]}})
    elif status:
        clauses.append({"status": status})
    if category:
        clauses.append({"category": category})
    text = (q or "").strip()
    if text:
        pattern = re.escape(text)
        clauses.append(
            {
                "$or": [
                    {"name": {"$regex": pattern, "$options": "i"}},
                    {"city": {"$regex": pattern, "$options": "i"}},
                    {"address": {"$regex": pattern, "$options": "i"}},
                ]
            }
        )
    if ownership == "owned":
        clauses.append(
            {"$or": [{"owner_uid": {"$type": "string"}}, {"_id": {"$in": owner_oids}}]}
        )
    elif ownership == "unowned":
        clauses.append({"owner_uid": {"$not": {"$type": "string"}}})
        clauses.append({"_id": {"$nin": owner_oids}})
    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


@router.get("/admin/businesses/summary", response_model=BusinessCounts)
async def business_summary(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Counts for the admin dashboard. Deleted includes owner and admin removals."""
    _require_admin(current_user)
    counts = {"pending_review": 0, "published": 0, "rejected": 0}
    rows = await db.businesses.aggregate(
        [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]
    ).to_list(None)
    for row in rows:
        if row.get("_id") in counts:
            counts[row["_id"]] = row["n"]
    deleted = await db.business_deletions.count_documents({})
    return BusinessCounts(
        pending=counts["pending_review"],
        published=counts["published"],
        rejected=counts["rejected"],
        deleted=deleted,
    )


@router.get("/admin/businesses/page", response_model=BusinessPage)
async def page_businesses(
    limit: int = Query(default=20, ge=1, le=50),
    skip: int = Query(default=0, ge=0),
    status: str | None = None,
    category: str | None = None,
    q: str | None = None,
    ownership: Literal["all", "owned", "unowned"] = "all",
    sort: Literal["category", "name", "city", "recent"] = "category",
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """One page of listings. The next page is requested as the list scrolls."""
    _require_admin(current_user)
    allowed_status = {"pending_review", "published", "rejected", "suspended", "reviewed"}
    allowed_category = {"veterinarian", "groomer", "pharmacy", "pet_friendly", "pet_store"}
    if status and status not in allowed_status:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    if category and category not in allowed_category:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    owner_oids: list[ObjectId] = []
    if ownership != "all":
        rows = await db.business_members.find({"role": "owner"}, {"business_id": 1}).to_list(None)
        for row in rows:
            business_id = str(row.get("business_id") or "")
            if ObjectId.is_valid(business_id):
                owner_oids.append(ObjectId(business_id))
    match = _page_match(status, category, q, ownership, owner_oids)
    if sort == "name":
        sort_spec = [("name", 1), ("_id", 1)]
    elif sort == "city":
        sort_spec = [("city", 1), ("name", 1), ("_id", 1)]
    elif sort == "recent":
        sort_spec = [("updated_at", -1), ("_id", -1)]
    else:
        sort_spec = None
    if sort_spec is None:
        pipeline = [
            *([{"$match": match}] if match else []),
            {
                "$addFields": {
                    "_rank": {
                        "$switch": {
                            "branches": [
                                {"case": {"$eq": ["$category", "veterinarian"]}, "then": 0},
                                {"case": {"$eq": ["$category", "groomer"]}, "then": 1},
                                {"case": {"$eq": ["$category", "pharmacy"]}, "then": 2},
                                {"case": {"$eq": ["$category", "pet_friendly"]}, "then": 3},
                                {"case": {"$eq": ["$category", "pet_store"]}, "then": 4},
                            ],
                            "default": 9,
                        }
                    }
                }
            },
            {"$sort": {"_rank": 1, "name": 1, "_id": 1}},
            {"$skip": skip},
            {"$limit": limit + 1},
        ]
        docs = await db.businesses.aggregate(pipeline).to_list(None)
    else:
        docs = await db.businesses.find(match).sort(sort_spec).skip(skip).limit(limit + 1).to_list(None)
    has_more = len(docs) > limit
    return BusinessPage(items=await _decorate(db, docs[:limit]), has_more=has_more)


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
    return await _decorate(db, docs)


async def _business_or_404(db: AsyncIOMotorDatabase, business_id: str) -> dict:
    if not ObjectId.is_valid(business_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    doc = await db.businesses.find_one({"_id": ObjectId(business_id)})
    if not doc:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    return doc


@router.post("/admin/businesses/{business_id}/invitations", response_model=InvitationOut, status_code=201)
async def admin_invite_owner(
    business_id: str,
    body: OwnerInvite,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Email someone a request to become the owner. Membership waits for their approval."""
    admin_email = _require_admin(current_user)
    business = await _business_or_404(db, business_id)
    invite = await _create_invitation(db, business, body.email, "owner", admin_email)
    return _invite_out(invite, business.get("name") or "")


@router.post("/businesses/{business_id}/invitations", response_model=InvitationOut, status_code=201)
async def owner_invite_member(
    business_id: str,
    body: TeamInvite,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """An owner invites another owner or a worker. They must approve before joining."""
    business = await _business_or_404(db, business_id)
    member = await db.business_members.find_one(
        {"business_id": str(business["_id"]), "user_id": current_user["uid"]}
    )
    if not member or member.get("role") != "owner":
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    invite = await _create_invitation(
        db,
        business,
        body.email,
        body.role,
        (current_user.get("email") or "").strip().lower(),
    )
    return _invite_out(invite, business.get("name") or "")


@router.post("/businesses/invitations/{invitation_id}/approve", response_model=InvitationOut)
async def approve_invitation(
    invitation_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """The invited person accepts and becomes a business_members row."""
    return await _respond_invitation(invitation_id, current_user, db, accepted=True)


@router.post("/businesses/invitations/{invitation_id}/decline", response_model=InvitationOut)
async def decline_invitation(
    invitation_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """The invited person declines. No membership is created."""
    return await _respond_invitation(invitation_id, current_user, db, accepted=False)


async def _respond_invitation(
    invitation_id: str,
    current_user: dict,
    db: AsyncIOMotorDatabase,
    *,
    accepted: bool,
) -> InvitationOut:
    if not ObjectId.is_valid(invitation_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    invite = await db.business_invitations.find_one({"_id": ObjectId(invitation_id)})
    if not invite:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    email = (current_user.get("email") or "").strip().lower()
    if invite.get("email") != email:
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    if invite.get("status") != "pending":
        raise_api_error(400, ErrorCode.ALREADY_RESOLVED)
    business = await _business_or_404(db, str(invite.get("business_id")))
    now = datetime.now(timezone.utc)
    status = "approved" if accepted else "declined"
    await db.business_invitations.update_one(
        {"_id": invite["_id"]},
        {"$set": {"status": status, "updated_at": now, "responded_at": now}},
    )
    invite["status"] = status
    if accepted:
        uid = current_user["uid"]
        await db.business_members.update_one(
            {"business_id": str(business["_id"]), "user_id": uid},
            {
                "$set": {
                    "business_id": str(business["_id"]),
                    "user_id": uid,
                    "email": email,
                    "role": invite.get("role"),
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        if invite.get("role") == "owner" and not business.get("owner_uid"):
            other = await db.businesses.find_one({"owner_uid": uid})
            if not other:
                await db.businesses.update_one(
                    {"_id": business["_id"]},
                    {"$set": {"owner_uid": uid, "owner_email": email, "updated_at": now}},
                )
    return _invite_out(invite, business.get("name") or "")


@router.delete("/admin/businesses/{business_id}", status_code=204)
async def admin_delete_business(
    business_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Ragly admins can remove a listing, its members, and its invitations."""
    _require_admin(current_user)
    await _delete_business(db, await _business_or_404(db, business_id))


@router.post("/admin/businesses/{business_id}/photos", response_model=BusinessOut)
async def admin_add_business_photo(
    business_id: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Store a listing photo in Firebase and save its URL on the business."""
    _require_admin(current_user)
    business = await _business_or_404(db, business_id)
    return await _add_business_photo(db, business, file)


@router.delete("/admin/businesses/{business_id}/photos", response_model=BusinessOut)
async def admin_remove_business_photo(
    business_id: str,
    body: PhotoRemove,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    _require_admin(current_user)
    business = await _business_or_404(db, business_id)
    return await _remove_business_photo(db, business, body.url)


@router.post("/businesses/{business_id}/photos", response_model=BusinessOut)
async def owner_add_business_photo(
    business_id: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Owners can add listing photos. Workers cannot."""
    business = await _business_or_404(db, business_id)
    if not await _listing_owner(db, business, current_user["uid"]):
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    return await _add_business_photo(db, business, file)


@router.delete("/businesses/{business_id}/photos", response_model=BusinessOut)
async def owner_remove_business_photo(
    business_id: str,
    body: PhotoRemove,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    business = await _business_or_404(db, business_id)
    if not await _listing_owner(db, business, current_user["uid"]):
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    return await _remove_business_photo(db, business, body.url)


@router.delete("/businesses/{business_id}", status_code=204)
async def owner_delete_business(
    business_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Only an owner member can delete the business. A worker cannot."""
    business = await _business_or_404(db, business_id)
    member = await db.business_members.find_one(
        {"business_id": str(business["_id"]), "user_id": current_user["uid"]}
    )
    if not member or member.get("role") != "owner":
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    await _delete_business(db, business)


@router.patch("/businesses/{business_id}", response_model=BusinessOut)
async def owner_update_business(
    business_id: str,
    body: BusinessSubmit,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Owners can edit the public listing. Workers cannot."""
    business = await _business_or_404(db, business_id)
    member = await db.business_members.find_one(
        {"business_id": str(business["_id"]), "user_id": current_user["uid"]}
    )
    if not member or member.get("role") != "owner":
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    now = datetime.now(timezone.utc)
    fields = _public_fields(body)
    fields["updated_at"] = now
    unset = {"phones": "", "latitude": "", "longitude": "", "photo_url": ""}
    if "instagram" not in fields:
        unset["instagram"] = ""
    await db.businesses.update_one({"_id": business["_id"]}, {"$set": fields, "$unset": unset})
    business.update(fields)
    if "instagram" not in fields:
        business.pop("instagram", None)
    updated = _to_out(business)
    invitations = await _load_invitations(db, [updated.id])
    owner_ids = await _owner_business_ids(db, [updated.id])
    return _apply_team(updated, invitations.get(updated.id, []), owner_ids)


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
                "field_errors": {},
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
    doc["field_errors"] = {}
    doc["updated_at"] = now
    _email_owner(
        doc.get("owner_email"),
        business_name=doc.get("name") or "",
        subject=f"Ragly: {doc.get('name') or 'Your business'} is published",
        intro=f"{doc.get('name') or 'Your business'} is published. Pet owners can find it in the app.",
    )
    return _to_out(doc)


@router.post("/admin/businesses/{business_id}/reject", response_model=BusinessOut)
async def reject_business(
    business_id: str,
    body: BusinessReject,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Keep the listing off the app and store a note on each field the owner must fix."""
    admin_email = _require_admin(current_user)
    if not ObjectId.is_valid(business_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    doc = await db.businesses.find_one({"_id": ObjectId(business_id)})
    if not doc:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    if doc.get("status") != "pending_review":
        raise_api_error(400, ErrorCode.BUSINESS_NOT_PENDING)
    now = datetime.now(timezone.utc)
    summary = "\n".join(f"{key}: {message}" for key, message in body.field_errors.items())
    await db.businesses.update_one(
        {"_id": doc["_id"]},
        {
            "$set": {
                "status": "rejected",
                "rejection_reason": summary,
                "field_errors": body.field_errors,
                "reviewed_at": now,
                "reviewed_by": admin_email,
                "updated_at": now,
            }
        },
    )
    doc["status"] = "rejected"
    doc["rejection_reason"] = summary
    doc["field_errors"] = body.field_errors
    doc["updated_at"] = now
    _email_owner(
        doc.get("owner_email"),
        business_name=doc.get("name") or "",
        subject=f"Ragly: {doc.get('name') or 'Your business'} needs changes",
        intro=f"{doc.get('name') or 'Your business'} was not published. Fix the fields below and send it again.",
        lines=[f"{key}: {message}" for key, message in body.field_errors.items()],
    )
    return _to_out(doc)
