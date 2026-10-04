"""
Business publish requests.

An owner submits one listing. Ragly admins, identified by RAGLY_ADMIN_EMAILS,
approve or reject it. A new pending request emails every admin.
"""

import logging
import math
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
from app.core.utils import doc_to_dict, is_valid_object_id
from app.locations import (
    ensure_locations,
    list_location_docs,
    location_out,
    location_payload,
    locations_for,
    mirror_fields,
    place_location,
)
from app.middleware.auth import get_current_user
from app.models.business import (
    AdminPublish,
    BusinessCounts,
    BusinessMembership,
    BusinessOut,
    BusinessPage,
    BusinessPlace,
    BusinessPlaceDetail,
    BusinessPlacePage,
    PlaceReview,
    ReviewWrite,
    BusinessReject,
    BusinessSession,
    BusinessSubmit,
    InvitationOut,
    OpeningHours,
    LocationOut,
    LocationWrite,
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
            if not _TIME.match(slot.open) or not _is_clock(slot.close, end=True):
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
        role=data.get("role") if data.get("role") in {"owner", "branch_owner", "lead", "worker"} else "worker",
        status=data.get("status"),
        location_id=_text(data.get("location_id")),
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
    location_id: str | None = None,
    reports_to: str | None = None,
) -> dict:
    email = email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    business_id = str(business["_id"])
    member = await db.business_members.find_one({"business_id": business_id, "email": email})
    if member:
        raise_api_error(400, ErrorCode.ALREADY_RESOLVED)
    pending = await db.business_invitations.find_one(
        {
            "business_id": business_id,
            "email": email,
            "status": "pending",
            "location_id": location_id,
        }
    )
    if pending:
        raise_api_error(400, ErrorCode.ALREADY_RESOLVED)
    now = datetime.now(timezone.utc)
    payload = {
        "business_id": business_id,
        "email": email,
        "role": role,
        "location_id": location_id,
        "reports_to": reports_to,
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
    await db.business_locations.delete_many({"business_id": business_id})
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


async def _with_locations(db: AsyncIOMotorDatabase, business: BusinessOut, doc: dict) -> BusinessOut:
    rows = await ensure_locations(db, doc)
    business.locations = [location_out(row) for row in rows]
    return business


async def _sync_primary_location(db: AsyncIOMotorDatabase, business: dict) -> None:
    """The form still edits the first branch together with the brand."""
    rows = await ensure_locations(db, business)
    if not rows:
        return
    mirrored = mirror_fields(business)
    mirrored["updated_at"] = datetime.now(timezone.utc)
    await db.business_locations.update_one({"_id": rows[0]["_id"]}, {"$set": mirrored})


def _distance_between(
    latitude: float | None,
    longitude: float | None,
    point: dict | None,
) -> float | None:
    if not point or latitude is None or longitude is None:
        return None
    lng, lat = point["coordinates"]
    return round(_distance_km(latitude, longitude, lat, lng), 1)


@router.get("/businesses/mine", response_model=BusinessSession)
async def my_business(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Every business this account can open, including each branch."""
    uid = current_user["uid"]
    email = (current_user.get("email") or "").strip().lower()
    memberships = await db.business_members.find({"user_id": uid}).to_list(None)
    owned_docs = await db.businesses.find({"owner_uid": uid}).to_list(None)
    by_id: dict[str, dict] = {str(doc["_id"]): doc for doc in owned_docs}
    access: dict[str, tuple[str, str | None]] = {
        business_id: ("owner", None) for business_id in by_id
    }
    for member in memberships:
        business_id = str(member.get("business_id") or "")
        if not ObjectId.is_valid(business_id):
            continue
        role = member.get("role") if member.get("role") in {"owner", "branch_owner", "lead", "worker"} else "worker"
        location_id = _text(member.get("location_id"))
        if business_id not in by_id:
            doc = await db.businesses.find_one({"_id": ObjectId(business_id)})
            if not doc:
                continue
            by_id[business_id] = doc
        current = access.get(business_id)
        if current and current[0] == "owner":
            continue
        if role == "owner":
            access[business_id] = ("owner", None)
        elif business_id not in access:
            access[business_id] = (role, location_id)
    entries: list[BusinessMembership] = []
    for business_id, doc in by_id.items():
        role, location_id = access.get(business_id, ("owner", None))
        listed = await _with_locations(db, _to_out(doc), doc)
        invitations = await _load_invitations(db, [listed.id])
        owner_ids = await _owner_business_ids(db, [listed.id])
        _apply_team(listed, invitations.get(listed.id, []), owner_ids)
        if role != "owner" and location_id:
            listed.locations = [item for item in listed.locations if item.id == location_id]
        entries.append(BusinessMembership(business=listed, role=role, location_id=location_id))
    entries.sort(key=lambda item: item.business.name.lower())
    first = entries[0].business if entries else None
    # A listing the person submitted is not an owner seat until it is published
    # or an invitation is approved. Pending review keeps the old empty role.
    confirmed = [
        item.role
        for item in entries
        if item.business.status == "published" or item.business.id in {
            str(member.get("business_id"))
            for member in memberships
            if member.get("role")
        }
    ]
    first_role = confirmed[0] if confirmed else None
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
        business=first,
        businesses=entries,
        role=first_role,
        invitations=pending,
    )


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    arc = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(arc))


def _place_image(doc: dict) -> str | None:
    for url in _gallery(doc):
        return url
    photo = _text(doc.get("photo")) or _text(doc.get("photo_url"))
    if photo and photo.startswith("https://"):
        return photo
    return None


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _is_clock(value: str, *, end: bool = False) -> bool:
    """A HH:MM clock. A closing time may be 24:00, the end of that day."""
    if end and value == "24:00":
        return True
    return bool(_TIME.match(value))


def _minutes(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    if value == "24:00":
        return 24 * 60
    if not _TIME.match(value):
        return None
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _schedule(doc: dict) -> dict:
    """Open or closed in the business timezone, plus the next change."""
    closed = {
        "open_now": False,
        "open_24_7": False,
        "closes_at": None,
        "opens_at": None,
        "next_open_day": None,
        "opens_tomorrow": False,
    }
    hours = _hours(doc)
    if hours["always_open"]:
        return {**closed, "open_now": True, "open_24_7": True}
    tz_name = _text(doc.get("timezone")) or "Europe/Chisinau"
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("Europe/Chisinau")
    now = datetime.now(zone)
    today = now.weekday()
    now_min = now.hour * 60 + now.minute

    def slots_for(index: int) -> list[tuple[int, int]]:
        parsed: list[tuple[int, int]] = []
        for slot in hours[_DAYS[index]]:
            start = _minutes(slot.get("open"))
            end = _minutes(slot.get("close"))
            if start is None or end is None or end <= start:
                continue
            parsed.append((start, end))
        return parsed

    def full_day(index: int) -> bool:
        return any(start == 0 and end >= 24 * 60 - 1 for start, end in slots_for(index))

    if all(full_day(index) for index in range(7)):
        return {**closed, "open_now": True, "open_24_7": True}

    for start, end in slots_for(today):
        if start <= now_min < end:
            return {**closed, "open_now": True, "closes_at": _clock(end)}
    later = [start for start, _end in slots_for(today) if start > now_min]
    if later:
        return {**closed, "opens_at": _clock(min(later))}
    for offset in range(1, 8):
        index = (today + offset) % 7
        upcoming = slots_for(index)
        if not upcoming:
            continue
        return {
            **closed,
            "opens_at": _clock(min(start for start, _end in upcoming)),
            "next_open_day": _DAYS[index],
            "opens_tomorrow": offset == 1,
        }
    return closed


async def _ratings(db: AsyncIOMotorDatabase, business_ids: list[str]) -> dict[str, float]:
    """Average star rating from business_reviews. Missing reviews stay unset."""
    if not business_ids:
        return {}
    rows = await db.business_reviews.find({"business_id": {"$in": business_ids}}).to_list(None)
    totals: dict[str, list[float]] = {}
    for row in rows:
        business_id = str(row.get("business_id") or "")
        rating = row.get("rating")
        if not business_id or not isinstance(rating, (int, float)):
            continue
        totals.setdefault(business_id, []).append(float(rating))
    return {
        business_id: round(sum(values) / len(values), 1)
        for business_id, values in totals.items()
        if values
    }


@router.get("/businesses/nearby", response_model=BusinessPlacePage)
async def nearby_businesses(
    latitude: float | None = None,
    longitude: float | None = None,
    limit: int = Query(default=15, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    anywhere: bool = Query(default=False),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """One card per business. With coordinates, the card is the closest branch."""
    del current_user
    docs = await db.businesses.find({"status": "published"}).to_list(500)
    located = await locations_for(db, [str(doc["_id"]) for doc in docs])
    places: list[BusinessPlace] = []
    for doc in docs:
        data = doc_to_dict(doc)
        if data.get("category") not in {
            "veterinarian",
            "groomer",
            "pharmacy",
            "pet_friendly",
            "pet_store",
        }:
            continue
        branches = located.get(data["id"]) or await ensure_locations(db, doc)
        chosen = doc
        chosen_point = _location(doc)
        distance = _distance_between(latitude, longitude, chosen_point)
        for branch in branches:
            point = branch.get("location") if isinstance(branch.get("location"), dict) else None
            branch_distance = _distance_between(latitude, longitude, point)
            if branch_distance is None:
                if chosen_point is None and point:
                    chosen = branch
                    chosen_point = point
                continue
            if distance is None or branch_distance < distance:
                chosen = branch
                chosen_point = point
                distance = branch_distance
        schedule = _schedule(chosen if chosen.get("opening_hours") else doc)
        places.append(
            BusinessPlace(
                id=data["id"],
                name=_text(data.get("name")) or "",
                category=data.get("category"),
                city=_text(chosen.get("city")) or _text(data.get("city")) or "",
                image=_place_image(doc),
                distance_km=distance,
                address=_text(chosen.get("address")) or _text(data.get("address")) or "",
                location_count=max(len(branches), 1),
                **schedule,
            )
        )
    ratings = await _ratings(db, [place.id for place in places])
    if ratings:
        places = [
            place.model_copy(update={"rating": ratings.get(place.id)})
            for place in places
        ]
    if not anywhere and latitude is not None and longitude is not None:
        places.sort(key=lambda place: (place.distance_km is None, place.distance_km or 0))
    else:
        places.sort(key=lambda place: place.name.lower())
    page = places[offset:offset + limit]
    return BusinessPlacePage(items=page, has_more=offset + limit < len(places))


REVIEW_PAGE_SIZE = 15


async def _place_reviews(
    db: AsyncIOMotorDatabase,
    business_id: str,
    uid: str,
    *,
    limit: int = REVIEW_PAGE_SIZE,
    cursor: str | None = None,
) -> list[PlaceReview]:
    """Newest reviews first. `cursor` is the last id from the previous page."""
    query: dict = {"business_id": business_id}
    if cursor and is_valid_object_id(cursor):
        last = await db.business_reviews.find_one(
            {"_id": ObjectId(cursor), "business_id": business_id}
        )
        created_at = last.get("created_at") if last else None
        if last and isinstance(created_at, datetime):
            last_id = last["_id"]
            query["$or"] = [
                {"created_at": {"$lt": created_at}},
                {"created_at": created_at, "_id": {"$lt": last_id}},
            ]
    rows = (
        await db.business_reviews.find(query)
        .sort([("created_at", -1), ("_id", -1)])
        .limit(limit)
        .to_list(limit)
    )
    author_ids = [
        str(row.get("user_id"))
        for row in rows
        if isinstance(row.get("user_id"), str) and row.get("user_id")
    ]
    authors: dict[str, dict] = {}
    if author_ids:
        people = await db.users.find({"firebase_uid": {"$in": author_ids}}).to_list(None)
        authors = {
            str(person.get("firebase_uid")): person
            for person in people
            if person.get("firebase_uid")
        }
    reviews: list[PlaceReview] = []
    for row in rows:
        rating = row.get("rating")
        created_at = row.get("created_at")
        if not isinstance(rating, int) or not isinstance(created_at, datetime):
            continue
        if rating < 1 or rating > 5:
            continue
        author = authors.get(str(row.get("user_id") or ""), {})
        photo = author.get("photo_url")
        reviews.append(
            PlaceReview(
                id=str(row.get("_id")),
                author_name=_text(author.get("name")) or "",
                author_photo=photo if isinstance(photo, str) and photo.startswith("https://") else None,
                rating=rating,
                comment=_text(row.get("comment")),
                created_at=created_at,
                is_mine=str(row.get("user_id") or "") == uid,
            )
        )
    return reviews


def _phones(doc: dict) -> list[str]:
    raw = doc.get("phone")
    if isinstance(raw, list):
        return [phone.strip() for phone in raw if isinstance(phone, str) and phone.strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


@router.get("/businesses/{business_id}", response_model=BusinessPlaceDetail)
async def business_place(
    business_id: str,
    latitude: float | None = None,
    longitude: float | None = None,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """One published listing for the business screen."""
    uid = current_user["uid"]
    try:
        oid = ObjectId(business_id)
    except Exception:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    doc = await db.businesses.find_one({"_id": oid, "status": "published"})
    if not doc:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    data = doc_to_dict(doc)
    branches = await ensure_locations(db, doc)
    business_phone = _phones(doc)
    public_branches = []
    closest = None
    closest_distance = None
    for branch in branches:
        point = branch.get("location") if isinstance(branch.get("location"), dict) else None
        distance = _distance_between(latitude, longitude, point)
        public = place_location(branch, business_phone, distance)
        public_branches.append(public)
        if distance is not None and (closest_distance is None or distance < closest_distance):
            closest = public
            closest_distance = distance
    if closest is None and public_branches:
        closest = public_branches[0]
    point = closest.location.model_dump() if closest and closest.location else _location(doc)
    schedule_doc = doc
    if closest:
        match = next((branch for branch in branches if str(branch.get("_id")) == closest.id), None)
        if match:
            schedule_doc = match
    schedule = _schedule(schedule_doc if schedule_doc.get("opening_hours") else doc)
    ratings = await _ratings(db, [data["id"]])
    reviews = await _place_reviews(db, data["id"], uid, limit=REVIEW_PAGE_SIZE)
    location = None
    if isinstance(point, dict) and isinstance(point.get("coordinates"), list):
        location = {"type": "Point", "coordinates": point["coordinates"]}
    return BusinessPlaceDetail(
        id=data["id"],
        name=_text(data.get("name")) or "",
        category=data.get("category"),
        image=_place_image(doc),
        rating=ratings.get(data["id"]),
        description=_text(data.get("description")),
        address=closest.address if closest else (_text(data.get("address")) or ""),
        phone=business_phone,
        website=_text(data.get("website")),
        instagram=_text(data.get("instagram")),
        opening_hours=closest.opening_hours if closest else _hours(doc),
        location=location,
        reviews=reviews,
        locations=public_branches,
        location_count=max(len(public_branches), 1),
        distance_km=closest_distance if closest_distance is not None else (
            closest.distance_km if closest else None
        ),
        city=closest.city if closest and closest.city else (_text(data.get("city")) or ""),
        **schedule,
    )


@router.get("/businesses/{business_id}/reviews", response_model=list[PlaceReview])
async def list_business_reviews(
    business_id: str,
    limit: int = Query(REVIEW_PAGE_SIZE, ge=1, le=REVIEW_PAGE_SIZE),
    cursor: str | None = Query(None),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """One page of reviews, newest first. Pass the last id as `cursor` for the next page."""
    try:
        oid = ObjectId(business_id)
    except Exception:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    business = await db.businesses.find_one({"_id": oid, "status": "published"})
    if not business:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    return await _place_reviews(
        db,
        business_id,
        current_user["uid"],
        limit=limit,
        cursor=cursor,
    )


@router.post("/businesses/{business_id}/reviews", response_model=PlaceReview)
async def write_business_review(
    business_id: str,
    body: ReviewWrite,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Save this user's rating. A second save updates the same review."""
    try:
        oid = ObjectId(business_id)
    except Exception:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    business = await db.businesses.find_one({"_id": oid, "status": "published"})
    if not business:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    uid = current_user["uid"]
    now = datetime.now(timezone.utc)
    existing = await db.business_reviews.find_one({"business_id": business_id, "user_id": uid})
    if existing:
        await db.business_reviews.update_one(
            {"_id": existing["_id"]},
            {"$set": {"rating": body.rating, "comment": body.comment, "updated_at": now}},
        )
        created_at = existing.get("created_at") or now
        review_id = existing["_id"]
    else:
        created_at = now
        inserted = await db.business_reviews.insert_one(
            {
                "business_id": business_id,
                "user_id": uid,
                "rating": body.rating,
                "comment": body.comment,
                "created_at": now,
                "updated_at": now,
            }
        )
        review_id = inserted.inserted_id
    author = await db.users.find_one({"firebase_uid": uid}) or {}
    photo = author.get("photo_url")
    return PlaceReview(
        id=str(review_id),
        author_name=_text(author.get("name")) or "",
        author_photo=photo if isinstance(photo, str) and photo.startswith("https://") else None,
        rating=body.rating,
        comment=body.comment,
        created_at=created_at if isinstance(created_at, datetime) else now,
        is_mine=True,
    )


@router.post("/businesses", response_model=BusinessOut, status_code=201)
async def submit_business(
    body: BusinessSubmit,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Create another listing for this account and email the admins."""
    if settings.is_ragly_admin(current_user.get("email")):
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    fields = _public_fields(body)
    uid = current_user["uid"]
    owner_email = (current_user.get("email") or "").strip().lower()
    now = datetime.now(timezone.utc)
    rejected = await db.businesses.find_one({"owner_uid": uid, "status": "rejected"})
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
    if rejected:
        await db.businesses.update_one({"_id": rejected["_id"]}, {"$set": payload})
        rejected.update(payload)
        doc = rejected
        await _sync_primary_location(db, doc)
    else:
        payload["created_at"] = now
        result = await db.businesses.insert_one(payload)
        payload["_id"] = result.inserted_id
        doc = payload
        await ensure_locations(db, doc)

    _notify_admins(fields, owner_email)
    listed = await _with_locations(db, _to_out(doc), doc)
    _email_owner(
        owner_email,
        business_name=fields["name"],
        subject=f"Ragly: {fields['name']} is waiting for review",
        intro=f"We received {fields['name']}. It stays pending until a Ragly admin approves it.",
    )
    return listed


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
    await ensure_locations(db, payload)
    business = await _with_locations(db, _to_out(payload), payload)
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
    await _sync_primary_location(db, doc)
    return await _with_locations(db, _to_out(doc), doc)


async def _decorate(db: AsyncIOMotorDatabase, docs: list[dict]) -> list[BusinessOut]:
    businesses = [_to_out(doc) for doc in docs]
    business_ids = [business.id for business in businesses]
    invitations = await _load_invitations(db, business_ids)
    owner_ids = await _owner_business_ids(db, business_ids)
    grouped = await locations_for(db, business_ids)
    decorated = []
    for business in businesses:
        business.locations = [location_out(row) for row in grouped.get(business.id, [])]
        decorated.append(_apply_team(business, invitations.get(business.id, []), owner_ids))
    return decorated


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


async def _brand_owner(db: AsyncIOMotorDatabase, business: dict, current_user: dict) -> bool:
    if settings.is_ragly_admin(current_user.get("email")):
        return True
    return await _listing_owner(db, business, current_user["uid"])


@router.post("/businesses/{business_id}/locations", response_model=LocationOut, status_code=201)
async def add_location(
    business_id: str,
    body: LocationWrite,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Add a branch. The brand owner and Ragly admins can do this."""
    business = await _business_or_404(db, business_id)
    if not await _brand_owner(db, business, current_user):
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    try:
        ZoneInfo(body.timezone.strip())
    except ZoneInfoNotFoundError:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    now = datetime.now(timezone.utc)
    doc = {
        **location_payload(body),
        "business_id": str(business["_id"]),
        "created_at": now,
        "updated_at": now,
    }
    result = await db.business_locations.insert_one(doc)
    doc["_id"] = result.inserted_id
    return location_out(doc)


@router.patch("/businesses/{business_id}/locations/{location_id}", response_model=LocationOut)
async def update_location(
    business_id: str,
    location_id: str,
    body: LocationWrite,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Edit one branch. Its store manager can edit that branch only."""
    business = await _business_or_404(db, business_id)
    if not ObjectId.is_valid(location_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    row = await db.business_locations.find_one(
        {"_id": ObjectId(location_id), "business_id": str(business["_id"])}
    )
    if not row:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    if not await _brand_owner(db, business, current_user):
        member = await db.business_members.find_one(
            {
                "business_id": str(business["_id"]),
                "user_id": current_user["uid"],
                "role": "branch_owner",
                "location_id": location_id,
            }
        )
        if not member:
            raise_api_error(403, ErrorCode.UNAUTHORIZED)
    try:
        ZoneInfo(body.timezone.strip())
    except ZoneInfoNotFoundError:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    fields = {**location_payload(body), "updated_at": datetime.now(timezone.utc)}
    await db.business_locations.update_one({"_id": row["_id"]}, {"$set": fields})
    row.update(fields)
    rows = await list_location_docs(db, str(business["_id"]))
    if rows and rows[0]["_id"] == row["_id"]:
        mirrored = mirror_fields(row)
        mirrored["updated_at"] = fields["updated_at"]
        await db.businesses.update_one({"_id": business["_id"]}, {"$set": mirrored})
    return location_out(row)


@router.delete("/businesses/{business_id}/locations/{location_id}", status_code=204)
async def delete_location(
    business_id: str,
    location_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Remove a branch. The last branch stays, because a business needs an address."""
    business = await _business_or_404(db, business_id)
    if not await _brand_owner(db, business, current_user):
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    if not ObjectId.is_valid(location_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    rows = await list_location_docs(db, str(business["_id"]))
    if len(rows) <= 1:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    await db.business_locations.delete_one(
        {"_id": ObjectId(location_id), "business_id": str(business["_id"])}
    )
    return None


@router.post("/businesses/{business_id}/invitations", response_model=InvitationOut, status_code=201)
async def owner_invite_member(
    business_id: str,
    body: TeamInvite,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Invite an owner for every branch, or a store manager, lead, or staff for one branch."""
    business = await _business_or_404(db, business_id)
    uid = current_user["uid"]
    is_owner = await _brand_owner(db, business, current_user)
    location_id = (body.location_id or "").strip() or None
    reports_to = (body.reports_to or "").strip() or None
    if body.role == "owner":
        if not is_owner:
            raise_api_error(403, ErrorCode.UNAUTHORIZED)
        location_id = None
        reports_to = None
    else:
        if not location_id:
            rows = await ensure_locations(db, business)
            location_id = str(rows[0]["_id"]) if rows else None
        if not location_id or not ObjectId.is_valid(location_id):
            raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
        branch = await db.business_locations.find_one(
            {"_id": ObjectId(location_id), "business_id": str(business["_id"])}
        )
        if not branch:
            raise_api_error(404, ErrorCode.NOT_FOUND)
        if not is_owner:
            mine = await db.business_members.find(
                {"business_id": str(business["_id"]), "user_id": uid, "location_id": location_id}
            ).to_list(None)
            roles = {row.get("role") for row in mine}
            if body.role == "branch_owner" or not roles.intersection({"branch_owner", "lead"}):
                raise_api_error(403, ErrorCode.UNAUTHORIZED)
            if "lead" in roles and "branch_owner" not in roles:
                reports_to = reports_to or uid
    invite = await _create_invitation(
        db,
        business,
        body.email,
        body.role,
        (current_user.get("email") or "").strip().lower(),
        location_id,
        reports_to,
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
        location_id = _text(invite.get("location_id"))
        await db.business_members.update_one(
            {
                "business_id": str(business["_id"]),
                "user_id": uid,
                "location_id": location_id,
            },
            {
                "$set": {
                    "business_id": str(business["_id"]),
                    "user_id": uid,
                    "email": email,
                    "role": invite.get("role"),
                    "location_id": location_id,
                    "reports_to": _text(invite.get("reports_to")),
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        if invite.get("role") == "owner" and not business.get("owner_uid"):
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
    """Owners can edit the public listing. A rejected listing goes back to review."""
    business = await _business_or_404(db, business_id)
    if not await _listing_owner(db, business, current_user["uid"]):
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    now = datetime.now(timezone.utc)
    fields = _public_fields(body)
    fields["updated_at"] = now
    if business.get("status") == "rejected":
        fields["status"] = "pending_review"
        fields["field_errors"] = {}
        fields["rejection_reason"] = None
        fields["submitted_at"] = now
    unset = {"phones": "", "latitude": "", "longitude": "", "photo_url": ""}
    if "instagram" not in fields:
        unset["instagram"] = ""
    await db.businesses.update_one({"_id": business["_id"]}, {"$set": fields, "$unset": unset})
    business.update(fields)
    if "instagram" not in fields:
        business.pop("instagram", None)
    await _sync_primary_location(db, business)
    updated = await _with_locations(db, _to_out(business), business)
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
