"""Businesses, their branches, and the review flow.

Owner flow: onboarding creates a draft business → the owner adds a branch
(address, pin, services with hours) → sends it for review → an admin approves
or sends it back with notes per field → the first approved branch publishes
the business. Admins create the same documents through one form.

Pet owners read `/businesses/nearby` and `/businesses/{id}`; those shapes do
not change when branches and services are added.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from typing import Literal

from bson import ObjectId
from fastapi import APIRouter, Depends, File, Query, UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app import business_docs as docs
from app.business_mail import note_lines, send_owner_update
from app.business_names import business_name_key
from app.core.config import settings
from app.core.database import get_database
from app.core.email_service import (
    EmailDeliveryError,
    send_business_invite_email,
    send_business_review_email,
    send_review_report_email,
)
from app.core.errors import ErrorCode, raise_api_error, raise_api_error_with
from app.core.firebase import delete_business_image, delete_business_images, upload_business_image
from app.core.utils import doc_to_dict, is_valid_object_id
from app.country import country_code
from app.geo import timezone_at
from app.images import identify_image
from app.middleware.auth import get_current_user
from app.models.business import (
    CATEGORIES,
    AdminApprove,
    AdminCreate,
    BranchApprove,
    BranchReject,
    BranchVisibility,
    BranchWrite,
    BusinessCounts,
    BusinessDetails,
    BusinessMembership,
    BusinessOut,
    BusinessPage,
    DirectoryFacets,
    BusinessPlace,
    BusinessPlaceDetail,
    BusinessPlacePage,
    BusinessReject,
    BusinessSession,
    EventOut,
    InvitationOut,
    ListingVisibility,
    LocationOut,
    NameCheck,
    NoticeSeen,
    OwnerInvite,
    PhotoRemove,
    PlaceConflict,
    PlaceReview,
    ReviewBranch,
    ReviewGroup,
    ReviewReportWrite,
    ReviewWrite,
    TeamInvite,
)

logger = logging.getLogger("petto")

router = APIRouter(tags=["businesses"])

_MAX_PHOTO_BYTES = 5 * 1024 * 1024
_ROLES = {"owner", "branch_owner", "lead", "worker"}
_NOTICE_LIMIT = 20
_CONFLICT_METERS = 60


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def _require_admin(current_user: dict) -> str:
    email = (current_user.get("email") or "").strip().lower()
    if not settings.is_ragly_admin(email):
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    return email


def _is_admin(current_user: dict) -> bool:
    return settings.is_ragly_admin(current_user.get("email"))


def _actor(current_user: dict) -> dict:
    return {"uid": current_user.get("uid"), "email": (current_user.get("email") or "").strip().lower()}


async def _business_or_404(db: AsyncIOMotorDatabase, business_id: str) -> dict:
    if not ObjectId.is_valid(business_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    doc = await db.businesses.find_one({"_id": ObjectId(business_id)})
    if not doc:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    return doc


async def _branch_or_404(db: AsyncIOMotorDatabase, business: dict, location_id: str) -> dict:
    if not ObjectId.is_valid(location_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    row = await db.business_locations.find_one(
        {"_id": ObjectId(location_id), "business_id": str(business["_id"])}
    )
    if not row:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    return row


async def _listing_owner(db: AsyncIOMotorDatabase, business: dict, uid: str) -> bool:
    """The account that created the business, or a member with the owner role."""
    if business.get("owner_uid") == uid:
        return True
    member = await db.business_members.find_one({"business_id": str(business["_id"]), "user_id": uid})
    return bool(member and member.get("role") == "owner")


async def _brand_owner(db: AsyncIOMotorDatabase, business: dict, current_user: dict) -> bool:
    if _is_admin(current_user):
        return True
    return await _listing_owner(db, business, current_user["uid"])


async def _require_brand_owner(db: AsyncIOMotorDatabase, business: dict, current_user: dict) -> None:
    if not await _brand_owner(db, business, current_user):
        raise_api_error(403, ErrorCode.UNAUTHORIZED)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _invite_out(doc: dict, business_name: str = "") -> InvitationOut:
    data = doc_to_dict(doc)
    return InvitationOut(
        id=data["id"],
        business_id=str(data.get("business_id") or ""),
        business_name=business_name,
        email=docs.text(data.get("email")),
        role=data.get("role") if data.get("role") in _ROLES else "worker",
        status=data.get("status") if data.get("status") in {"pending", "approved", "declined"} else "pending",
        location_id=docs.optional_text(data.get("location_id")),
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
    rows = await db.business_members.find({"business_id": {"$in": business_ids}, "role": "owner"}).to_list(None)
    return {str(row.get("business_id")) for row in rows}


async def _full_out(db: AsyncIOMotorDatabase, business: dict) -> BusinessOut:
    """The business with its branches, invitations, and ownership flag."""
    rows = await docs.ensure_branches(db, business)
    out = docs.business_out(business)
    out.locations = [docs.branch_out(row, business) for row in rows]
    invitations = await _load_invitations(db, [out.id])
    owner_ids = await _owner_business_ids(db, [out.id])
    out.invitations = invitations.get(out.id, [])
    if out.id in owner_ids:
        out.owned = True
    image = docs.profile_image(business, rows)
    if image:
        out.photo = image
    return out


async def _decorate(db: AsyncIOMotorDatabase, business_docs: list[dict]) -> list[BusinessOut]:
    business_ids = [str(doc["_id"]) for doc in business_docs]
    invitations = await _load_invitations(db, business_ids)
    owner_ids = await _owner_business_ids(db, business_ids)
    grouped = await docs.branches_for(db, business_ids)
    decorated: list[BusinessOut] = []
    for doc in business_docs:
        out = docs.business_out(doc)
        rows = grouped.get(out.id) or await docs.ensure_branches(db, doc)
        out.locations = [docs.branch_out(row, doc) for row in rows]
        out.invitations = invitations.get(out.id, [])
        if out.id in owner_ids:
            out.owned = True
        image = docs.profile_image(doc, rows)
        if image:
            out.photo = image
        decorated.append(out)
    return decorated


# ---------------------------------------------------------------------------
# Invitations and members
# ---------------------------------------------------------------------------


def _send_invite(email: str, business_name: str, role: str) -> None:
    accept_url = settings.BUSINESS_APP_URL.strip().rstrip("/") + "/dashboard"
    try:
        send_business_invite_email(email, business_name=business_name, role=role, accept_url=accept_url)
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
        {"business_id": business_id, "email": email, "status": "pending", "location_id": location_id}
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
    await docs.record_event(
        db, business=business, kind="owner_invited" if role == "owner" else "member_invited",
        audience="admins", actor={"email": invited_by}, message=email,
    )
    return payload


async def _upsert_owner_member(db: AsyncIOMotorDatabase, business: dict, uid: str, email: str | None, now: datetime) -> None:
    """The same member row whether the owner onboarded or accepted an invitation."""
    await db.business_members.update_one(
        {"business_id": str(business["_id"]), "user_id": uid, "location_id": None},
        {
            "$set": {
                "business_id": str(business["_id"]),
                "user_id": uid,
                "email": email,
                "role": "owner",
                "location_id": None,
                "reports_to": None,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def _delete_business(db: AsyncIOMotorDatabase, doc: dict) -> None:
    business_id = str(doc["_id"])
    await db.businesses.delete_one({"_id": doc["_id"]})
    await db.business_members.delete_many({"business_id": business_id})
    await db.business_invitations.delete_many({"business_id": business_id})
    await db.business_locations.delete_many({"business_id": business_id})
    await db.business_events.delete_many({"business_id": business_id})
    await db.business_deletions.insert_one({"business_id": business_id, "deleted_at": datetime.now(timezone.utc)})
    delete_business_images(business_id)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def _listing_has_owner(doc: dict) -> bool:
    return bool(docs.optional_text(doc.get("owner_uid")) or docs.optional_text(doc.get("owner_email")))


async def _listing_is_claimable(db: AsyncIOMotorDatabase, doc: dict) -> bool:
    """Unowned listings can be claimed by contacting us. An owner, member, or pending owner invite is not claimable."""
    if _listing_has_owner(doc):
        return False
    business_id = str(doc.get("_id") or "")
    if not business_id:
        return True
    if await db.business_members.find_one({"business_id": business_id, "role": "owner"}):
        return False
    if await db.business_invitations.find_one({"business_id": business_id, "role": "owner", "status": "pending"}):
        return False
    return True


async def _business_with_name(
    db: AsyncIOMotorDatabase, name: str, except_id: ObjectId | None = None
) -> dict | None:
    key = business_name_key(name)
    if not key:
        return None
    query: dict = {"name_key": key}
    if except_id is not None:
        query["_id"] = {"$ne": except_id}
    found = await db.businesses.find_one(query)
    if found:
        return found
    legacy: dict = {"name_key": {"$exists": False}}
    if except_id is not None:
        legacy["_id"] = {"$ne": except_id}
    for doc in await db.businesses.find(legacy, {"name": 1, "owner_uid": 1, "owner_email": 1}).to_list(None):
        if business_name_key(str(doc.get("name") or "")) == key:
            return doc
    return None


async def _name_taken(db: AsyncIOMotorDatabase, name: str, except_id: ObjectId | None = None) -> bool:
    return await _business_with_name(db, name, except_id) is not None


async def _ensure_unique_name(db: AsyncIOMotorDatabase, name: str, except_id: ObjectId | None = None) -> None:
    if not business_name_key(name):
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    existing = await _business_with_name(db, name, except_id)
    if existing is None:
        return
    raise_api_error_with(
        409,
        ErrorCode.BUSINESS_NAME_TAKEN,
        {"claimable": await _listing_is_claimable(db, existing)},
    )


@router.get("/businesses/name-check", response_model=NameCheck)
async def check_business_name(
    name: str = Query(min_length=1, max_length=200),
    business_id: str | None = Query(default=None),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Whether this name is free, while the owner is still typing it."""
    del current_user
    except_id = ObjectId(business_id) if business_id and ObjectId.is_valid(business_id) else None
    key = business_name_key(name)
    if not key:
        return NameCheck(available=False, name_key="", claimable=False)
    existing = await _business_with_name(db, name, except_id)
    if existing is None:
        return NameCheck(available=True, name_key=key, claimable=False)
    return NameCheck(available=False, name_key=key, claimable=await _listing_is_claimable(db, existing))


# ---------------------------------------------------------------------------
# Emails to admins
# ---------------------------------------------------------------------------


def _notify_admins(business: dict, branch: dict, owner_email: str) -> None:
    review_url = settings.BUSINESS_APP_URL.strip().rstrip("/") + "/admin"
    categories = ", ".join(docs.branch_categories(branch, business, public_only=False)) or "—"
    for admin_email in sorted(settings.ragly_admin_emails):
        try:
            send_business_review_email(
                admin_email,
                business_name=docs.text(business.get("name")),
                owner_email=owner_email,
                city=docs.text(branch.get("city")) or docs.text(branch.get("address")),
                category=categories,
                review_url=review_url,
            )
        except EmailDeliveryError:
            logger.exception("Review email failed for %s", admin_email)


async def _owner_contact(db: AsyncIOMotorDatabase, business: dict) -> tuple[str | None, str]:
    """Where to email the owner and in which language."""
    email = docs.optional_text(business.get("owner_email"))
    if not email:
        member = await db.business_members.find_one({"business_id": str(business["_id"]), "role": "owner"})
        email = docs.optional_text((member or {}).get("email"))
    return email, docs.business_locale(business)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


async def _notices(db: AsyncIOMotorDatabase, uid: str, business_ids: list[str]) -> list[EventOut]:
    if not business_ids:
        return []
    read = await db.business_notice_reads.find_one({"user_id": uid}) or {}
    seen_at = docs.utc(read.get("seen_at"))
    seen_ids = {str(item) for item in (read.get("seen_ids") or []) if item}
    rows = (
        await db.business_events.find({"business_id": {"$in": business_ids}, "audience": "owner"})
        .sort([("created_at", -1), ("_id", -1)])
        .limit(_NOTICE_LIMIT)
        .to_list(_NOTICE_LIMIT)
    )
    out: list[EventOut] = []
    for row in rows:
        created = docs.utc(row.get("created_at"))
        event_id = str(row.get("_id") or "")
        unread = event_id not in seen_ids and (seen_at is None or (created is not None and created > seen_at))
        out.append(docs.event_out(row, unread=unread))
    return out


@router.get("/businesses/mine", response_model=BusinessSession)
async def my_business(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Every business this account can open, with branches, invitations, and unread notices."""
    uid = current_user["uid"]
    email = (current_user.get("email") or "").strip().lower()
    memberships = await db.business_members.find({"user_id": uid}).to_list(None)
    owned_docs = await db.businesses.find({"owner_uid": uid}).to_list(None)
    by_id: dict[str, dict] = {str(doc["_id"]): doc for doc in owned_docs}
    access: dict[str, tuple[str, str | None]] = {business_id: ("owner", None) for business_id in by_id}
    for member in memberships:
        business_id = str(member.get("business_id") or "")
        if not ObjectId.is_valid(business_id):
            continue
        role = member.get("role") if member.get("role") in _ROLES else "worker"
        location_id = docs.optional_text(member.get("location_id"))
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
        listed = await _full_out(db, doc)
        if role != "owner" and location_id:
            listed.locations = [item for item in listed.locations if item.id == location_id]
        entries.append(BusinessMembership(business=listed, role=role, location_id=location_id))
    entries.sort(key=lambda item: (item.business.created_at or datetime.min.replace(tzinfo=timezone.utc)))
    first = entries[0].business if entries else None
    first_role = entries[0].role if entries else None
    pending_rows = await db.business_invitations.find({"email": email, "status": "pending"}).to_list(None) if email else []
    pending: list[InvitationOut] = []
    for row in pending_rows:
        business_name = ""
        if ObjectId.is_valid(str(row.get("business_id"))):
            invited = await db.businesses.find_one({"_id": ObjectId(row["business_id"])})
            if invited:
                business_name = invited.get("name") or ""
        pending.append(_invite_out(row, business_name))
    person = await db.users.find_one({"firebase_uid": uid}) or {}
    locale = docs.text(person.get("locale")).lower()[:2]
    if first is not None:
        locale = first.locale
    return BusinessSession(
        is_ragly_admin=_is_admin(current_user),
        business=first,
        businesses=entries,
        role=first_role,
        invitations=pending,
        notices=await _notices(db, uid, list(by_id)),
        locale=locale if locale in {"en", "ro", "ru", "he"} else "en",
    )


@router.post("/businesses/seen", status_code=204)
async def mark_business_notices_seen(
    body: NoticeSeen | None = None,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Mark one notice read, or the whole list when no event is sent."""
    now = datetime.now(timezone.utc)
    event_id = (body.event_id if body else None) or None
    if event_id:
        await db.business_notice_reads.update_one(
            {"user_id": current_user["uid"]},
            {"$set": {"user_id": current_user["uid"]}, "$addToSet": {"seen_ids": event_id}},
            upsert=True,
        )
        return
    await db.business_notice_reads.update_one(
        {"user_id": current_user["uid"]},
        {"$set": {"user_id": current_user["uid"], "seen_at": now}},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Public listing for pet owners
# ---------------------------------------------------------------------------


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    arc = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(arc))


def _distance_between(latitude: float | None, longitude: float | None, point: dict | None) -> float | None:
    pair = docs.lat_lng(point)
    if pair is None or latitude is None or longitude is None:
        return None
    return round(_distance_km(latitude, longitude, pair[0], pair[1]), 1)


def _place_image(doc: dict, branches: list[dict] | None = None) -> str | None:
    return docs.profile_image(doc, branches)


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


def _public_card_category(doc: dict, branches: list[dict], wanted: str | None) -> str | None:
    if wanted:
        return wanted
    for branch in branches:
        categories = docs.branch_categories(branch, doc, public_only=True)
        if categories:
            return categories[0]
    category = doc.get("category")
    return category if category in CATEGORIES else None


@router.get("/businesses/nearby", response_model=BusinessPlacePage)
async def nearby_businesses(
    latitude: float | None = None,
    longitude: float | None = None,
    limit: int = Query(default=15, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    anywhere: bool = Query(default=False),
    category: str | None = None,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """One card per live branch, closest first.

    A category filter keeps only branches that offer that service. Chips on
    every card are all live services of the business, not that branch alone.
    Hours on the card stay that branch's (or the filtered service's).
    """
    del current_user
    if category and category not in CATEGORIES:
        return BusinessPlacePage(items=[], has_more=False)
    query: dict = {"status": "published"}
    if category:
        query["$or"] = [{"categories": category}, {"category": category}]
    business_rows = await db.businesses.find(query).to_list(None)
    business_rows = [doc for doc in business_rows if docs.business_visible(doc)]
    grouped = await docs.branches_for(db, [str(doc["_id"]) for doc in business_rows])
    user_country = None
    if not anywhere and latitude is not None and longitude is not None:
        user_country = country_code(latitude, longitude)
    places: list[BusinessPlace] = []
    for doc in business_rows:
        business_id = str(doc["_id"])
        rows = grouped.get(business_id) or await docs.ensure_branches(db, doc)
        public = docs.public_branches(rows)
        business_cats = docs.public_business_categories(doc, public)
        if category:
            branches = [
                branch for branch in public
                if category in docs.branch_categories(branch, doc, public_only=True)
            ]
        else:
            branches = [branch for branch in public if docs.public_services(branch, doc)]
        for branch in branches:
            pair = docs.lat_lng(docs.point_doc(branch.get("location")))
            if user_country is not None and (pair is None or country_code(pair[0], pair[1]) != user_country):
                continue
            cats = business_cats or ([category] if category else [])
            card_category = (
                category
                if category and category in cats
                else (cats[0] if cats else _public_card_category(doc, [branch], category))
            )
            if card_category is None:
                continue
            places.append(
                BusinessPlace(
                    id=business_id,
                    name=docs.text(doc.get("name")),
                    category=card_category,
                    categories=cats or [card_category],
                    city=docs.text(branch.get("city")) or docs.text(doc.get("city")),
                    image=_place_image(doc, rows),
                    distance_km=_distance_between(latitude, longitude, docs.point_doc(branch.get("location"))),
                    address=docs.text(branch.get("address")) or docs.text(doc.get("address")),
                    location_count=1,
                    location_id=str(branch.get("_id")) if branch.get("_id") is not None else None,
                    **docs.card_schedule(branch, doc, category=category),
                )
            )
    if not anywhere and latitude is not None and longitude is not None:
        places.sort(key=lambda place: (place.distance_km is None, place.distance_km or 0, place.name.lower(), place.address.lower()))
    else:
        places.sort(key=lambda place: (place.name.lower(), place.address.lower()))
    page = places[offset:offset + limit]
    ratings = await _ratings(db, [place.id for place in page])
    if ratings:
        page = [place.model_copy(update={"rating": ratings.get(place.id)}) for place in page]
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
        last = await db.business_reviews.find_one({"_id": ObjectId(cursor), "business_id": business_id})
        created_at = last.get("created_at") if last else None
        if last and isinstance(created_at, datetime):
            query["$or"] = [
                {"created_at": {"$lt": created_at}},
                {"created_at": created_at, "_id": {"$lt": last["_id"]}},
            ]
    rows = (
        await db.business_reviews.find(query)
        .sort([("created_at", -1), ("_id", -1)])
        .limit(limit)
        .to_list(limit)
    )
    author_ids = [str(row.get("user_id")) for row in rows if isinstance(row.get("user_id"), str) and row.get("user_id")]
    authors: dict[str, dict] = {}
    if author_ids:
        people = await db.users.find({"firebase_uid": {"$in": author_ids}}).to_list(None)
        authors = {str(person.get("firebase_uid")): person for person in people if person.get("firebase_uid")}
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
                author_name=docs.text(author.get("name")),
                author_photo=photo if isinstance(photo, str) and photo.startswith("https://") else None,
                rating=rating,
                comment=docs.optional_text(row.get("comment")),
                created_at=docs.utc(created_at),
                is_mine=str(row.get("user_id") or "") == uid,
            )
        )
    return reviews


@router.get("/businesses/{business_id}", response_model=BusinessPlaceDetail)
async def business_place(
    business_id: str,
    latitude: float | None = None,
    longitude: float | None = None,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """One published business for the business screen, with every live branch."""
    uid = current_user["uid"]
    if not ObjectId.is_valid(business_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    doc = await db.businesses.find_one({"_id": ObjectId(business_id), "status": "published"})
    if not doc or not docs.business_visible(doc):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    branches = docs.public_branches(await docs.ensure_branches(db, doc))
    if latitude is not None and longitude is not None:
        user_country = country_code(latitude, longitude)
        if user_country is not None:
            in_country = []
            for branch in branches:
                pair = docs.lat_lng(docs.point_doc(branch.get("location")))
                if pair is not None and country_code(pair[0], pair[1]) == user_country:
                    in_country.append(branch)
            if in_country:
                branches = in_country
    business_phone = docs.phones(doc.get("phone"))
    public = []
    closest_branch: dict | None = None
    closest = None
    closest_distance = None
    for branch in branches:
        distance = _distance_between(latitude, longitude, docs.point_doc(branch.get("location")))
        listed = docs.place_branch(branch, doc, business_phone, distance, branches=branches)
        public.append(listed)
        if distance is not None and (closest_distance is None or distance < closest_distance):
            closest, closest_branch, closest_distance = listed, branch, distance
    public.sort(key=lambda item: (item.distance_km is None, item.distance_km or 0))
    if closest is None and public:
        closest = public[0]
        closest_branch = next((row for row in branches if str(row.get("_id")) == closest.id), branches[0])
    hours = docs.branch_hours(closest_branch, doc) if closest_branch else docs.hours_doc(doc.get("opening_hours"))
    zone = docs.branch_timezone(closest_branch, doc) if closest_branch else docs.valid_timezone(doc.get("timezone"))
    ratings = await _ratings(db, [business_id])
    reviews = await _place_reviews(db, business_id, uid, limit=REVIEW_PAGE_SIZE)
    categories: list[str] = []
    for branch in branches:
        for category in docs.branch_categories(branch, doc, public_only=True):
            if category not in categories:
                categories.append(category)
    primary = categories[0] if categories else (doc.get("category") if doc.get("category") in CATEGORIES else None)
    if primary is None:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    return BusinessPlaceDetail(
        id=business_id,
        name=docs.text(doc.get("name")),
        category=primary,
        categories=categories,
        image=_place_image(doc, branches),
        rating=ratings.get(business_id),
        description=docs.optional_text(doc.get("description")),
        address=closest.address if closest else docs.text(doc.get("address")),
        phone=business_phone,
        website=docs.optional_text(doc.get("website")),
        instagram=docs.optional_text(doc.get("instagram")),
        opening_hours=hours,
        location=closest.location if closest else docs.legacy_point(doc),
        reviews=reviews,
        locations=public,
        location_count=max(len(public), 1),
        location_id=closest.id if closest else None,
        distance_km=closest_distance if closest_distance is not None else (closest.distance_km if closest else None),
        city=closest.city if closest and closest.city else docs.text(doc.get("city")),
        **docs.public_schedule(hours, zone),
    )


# ---------------------------------------------------------------------------
# Reviews (pet owners)
# ---------------------------------------------------------------------------


async def _published_or_404(db: AsyncIOMotorDatabase, business_id: str) -> dict:
    if not ObjectId.is_valid(business_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    business = await db.businesses.find_one({"_id": ObjectId(business_id), "status": "published"})
    if not business:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    return business


@router.get("/businesses/{business_id}/reviews", response_model=list[PlaceReview])
async def list_business_reviews(
    business_id: str,
    limit: int = Query(REVIEW_PAGE_SIZE, ge=1, le=REVIEW_PAGE_SIZE),
    cursor: str | None = Query(None),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """One page of reviews, newest first. Pass the last id as `cursor` for the next page."""
    await _published_or_404(db, business_id)
    return await _place_reviews(db, business_id, current_user["uid"], limit=limit, cursor=cursor)


@router.post("/businesses/{business_id}/reviews", response_model=PlaceReview)
async def write_business_review(
    business_id: str,
    body: ReviewWrite,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Save this user's rating. A second save updates the same review."""
    await _published_or_404(db, business_id)
    uid = current_user["uid"]
    now = datetime.now(timezone.utc)
    existing = await db.business_reviews.find_one({"business_id": business_id, "user_id": uid})
    if existing:
        await db.business_reviews.update_one(
            {"_id": existing["_id"]},
            {"$set": {"rating": body.rating, "comment": body.comment, "updated_at": now}},
        )
        stored = existing.get("created_at")
        created_at = stored if isinstance(stored, datetime) else now
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
        author_name=docs.text(author.get("name")),
        author_photo=photo if isinstance(photo, str) and photo.startswith("https://") else None,
        rating=body.rating,
        comment=body.comment,
        created_at=docs.utc(created_at) or now,
        is_mine=True,
    )


@router.delete("/businesses/{business_id}/reviews", status_code=204)
async def delete_business_review(
    business_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Remove this user's review. Someone else's review stays."""
    await _published_or_404(db, business_id)
    existing = await db.business_reviews.find_one({"business_id": business_id, "user_id": current_user["uid"]})
    if not existing:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    await db.business_reviews.delete_one({"_id": existing["_id"]})


_REPORT_REASON = {
    "spam": "Spam or advertising",
    "offensive": "Offensive or inappropriate",
    "fake": "Fake or misleading",
    "irrelevant": "Irrelevant",
}


def _shown(value: object) -> str:
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(parts) if parts else "—"
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).isoformat()
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "—"
    text = str(value).strip()
    return text or "—"


def _person_lines(title: str, uid: str, person: dict | None, fallback_email: str = "") -> list[str]:
    person = person or {}
    email = person.get("email") or fallback_email
    return [
        title,
        f"User id: {_shown(uid)}",
        f"Name: {_shown(person.get('name'))}",
        f"Email: {_shown(email)}",
        f"Phone: {_shown(person.get('phone'))}",
        f"Photo: {_shown(person.get('photo_url'))}",
        f"Sign-in: {_shown(person.get('auth_provider'))}",
        f"Email verified: {_shown(person.get('email_verified')) if person else '—'}",
    ]


def _notify_review_report(lines: list[str], subject: str) -> None:
    recipients = set(settings.ragly_admin_emails)
    support = settings.SUPPORT_EMAIL.strip().lower()
    if support:
        recipients.add(support)
    if not recipients:
        logger.warning("No support email — review report was saved but not mailed\n%s", "\n".join(lines))
        return
    for admin_email in sorted(recipients):
        try:
            send_review_report_email(admin_email, subject=subject, lines=lines)
        except EmailDeliveryError:
            logger.exception("Review report email failed for %s", admin_email)


@router.post("/businesses/{business_id}/reviews/{review_id}/report", status_code=204)
async def report_business_review(
    business_id: str,
    review_id: str,
    body: ReviewReportWrite,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Email every Ragly admin the review, its author, and the person who reported it."""
    business = await _published_or_404(db, business_id)
    if not ObjectId.is_valid(review_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    review = await db.business_reviews.find_one({"_id": ObjectId(review_id), "business_id": business_id})
    if not review:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    reporter_uid = current_user["uid"]
    author_uid = str(review.get("user_id") or "")
    if not author_uid or author_uid == reporter_uid:
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    people = await db.users.find({"firebase_uid": {"$in": [reporter_uid, author_uid]}}).to_list(None)
    by_uid = {str(person.get("firebase_uid")): person for person in people if person.get("firebase_uid")}
    now = datetime.now(timezone.utc)
    reason_label = _REPORT_REASON[body.reason]
    business_name = docs.text(business.get("name")) or "Business"
    lines = [
        "A pet owner reported a review.",
        "",
        "Report",
        f"Reason: {reason_label} ({body.reason})",
        f"Reported at: {_shown(now)}",
        "",
        "Business",
        f"Business id: {business_id}",
        f"Name: {_shown(business.get('name'))}",
        f"Category: {_shown(business.get('category'))}",
        f"Status: {_shown(business.get('status'))}",
        f"City: {_shown(business.get('city'))}",
        f"Address: {_shown(business.get('address'))}",
        f"Phone: {_shown(business.get('phone'))}",
        f"Website: {_shown(business.get('website'))}",
        f"Instagram: {_shown(business.get('instagram'))}",
        "",
        "Review",
        f"Review id: {review_id}",
        f"Rating: {_shown(review.get('rating'))}",
        f"Comment: {_shown(review.get('comment'))}",
        f"Written at: {_shown(review.get('created_at'))}",
        f"Updated at: {_shown(review.get('updated_at'))}",
        "",
        *_person_lines("Review author", author_uid, by_uid.get(author_uid)),
        "",
        *_person_lines("Reporter", reporter_uid, by_uid.get(reporter_uid), fallback_email=str(current_user.get("email") or "")),
    ]
    result = await db.review_reports.insert_one(
        {
            "business_id": business_id,
            "review_id": review_id,
            "reason": body.reason,
            "reason_label": reason_label,
            "created_at": now,
            "business_name": business_name,
            "review_rating": review.get("rating"),
            "review_comment": review.get("comment"),
            "review_created_at": review.get("created_at"),
            "review_updated_at": review.get("updated_at"),
            "author_uid": author_uid,
            "author": {
                "name": (by_uid.get(author_uid) or {}).get("name"),
                "email": (by_uid.get(author_uid) or {}).get("email"),
                "phone": (by_uid.get(author_uid) or {}).get("phone"),
            },
            "reporter_uid": reporter_uid,
            "reporter": {
                "name": (by_uid.get(reporter_uid) or {}).get("name"),
                "email": (by_uid.get(reporter_uid) or {}).get("email") or current_user.get("email"),
                "phone": (by_uid.get(reporter_uid) or {}).get("phone"),
            },
        }
    )
    lines.extend(["", f"Report id: {result.inserted_id}"])
    _notify_review_report(lines, f"Ragly: review report — {business_name}")


# ---------------------------------------------------------------------------
# Onboarding and business details (owner)
# ---------------------------------------------------------------------------


@router.post("/businesses", response_model=BusinessOut, status_code=201)
async def create_business(
    body: BusinessDetails,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Onboarding: the business itself, as a draft. Branches come next; nothing is sent yet."""
    if _is_admin(current_user):
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    uid = current_user["uid"]
    owner_email = (current_user.get("email") or "").strip().lower() or None
    already = await db.businesses.find_one({"owner_uid": uid})
    if already is None:
        member = await db.business_members.find_one({"user_id": uid, "role": "owner"})
        already = member
    if already:
        raise_api_error(409, ErrorCode.BUSINESS_EXISTS)
    await _ensure_unique_name(db, body.name)
    now = datetime.now(timezone.utc)
    person = await db.users.find_one({"firebase_uid": uid}) or {}
    locale = body.locale or docs.text(person.get("locale")).lower()[:2] or "en"
    doc = docs.build_business(
        body, status="draft", source="owner", now=now,
        owner_uid=uid, owner_email=owner_email, locale=locale,
    )
    try:
        result = await db.businesses.insert_one(doc)
    except DuplicateKeyError:
        raise_api_error(409, ErrorCode.BUSINESS_NAME_TAKEN)
    doc["_id"] = result.inserted_id
    await _upsert_owner_member(db, doc, uid, owner_email, now)
    await docs.record_event(db, business=doc, kind="business_created", audience="admins", actor=_actor(current_user))
    return await _full_out(db, doc)


async def _apply_details(
    db: AsyncIOMotorDatabase,
    business: dict,
    body: BusinessDetails,
    current_user: dict,
) -> BusinessOut:
    await _ensure_unique_name(db, body.name, business["_id"])
    now = datetime.now(timezone.utc)
    updated = docs.build_business(
        body, status=docs.business_status(business), source="owner", now=now,
        owner_uid=None, owner_email=None, locale=docs.business_locale(business), existing=business,
    )
    changed = {
        key: updated[key]
        for key in ("name", "name_key", "phone", "email", "description", "website", "instagram", "locale", "onboarding_step", "updated_at")
    }
    unset = {key: "" for key in ("phones", "latitude", "longitude", "photo_url") if key in business}
    try:
        await db.businesses.update_one(
            {"_id": business["_id"]},
            {"$set": changed, **({"$unset": unset} if unset else {})},
        )
    except DuplicateKeyError:
        raise_api_error(409, ErrorCode.BUSINESS_NAME_TAKEN)
    for key in unset:
        business.pop(key, None)
    business.update(changed)
    await docs.record_event(
        db, business=business, kind="business_updated",
        audience="owner" if _is_admin(current_user) else "admins",
        actor=_actor(current_user),
    )
    return await _full_out(db, business)


@router.patch("/businesses/{business_id}", response_model=BusinessOut)
async def owner_update_business(
    business_id: str,
    body: BusinessDetails,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Edit the business itself: name, contacts, about. Branches are edited separately."""
    business = await _business_or_404(db, business_id)
    if not await _listing_owner(db, business, current_user["uid"]):
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    return await _apply_details(db, business, body, current_user)


@router.patch("/admin/businesses/{business_id}", response_model=BusinessOut)
async def admin_update_business(
    business_id: str,
    body: BusinessDetails,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Admins edit the same fields an owner can. Status and ownership stay."""
    _require_admin(current_user)
    business = await _business_or_404(db, business_id)
    return await _apply_details(db, business, body, current_user)


@router.patch("/businesses/{business_id}/visibility", response_model=BusinessOut)
async def set_business_visibility(
    business_id: str,
    body: ListingVisibility,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Hide or show the business in the app at once. Owners and admins."""
    business = await _business_or_404(db, business_id)
    await _require_brand_owner(db, business, current_user)
    if docs.business_status(business) != "published":
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    now = datetime.now(timezone.utc)
    update = {"visible": body.visible, "updated_at": now}
    await db.businesses.update_one({"_id": business["_id"]}, {"$set": update})
    business.update(update)
    await docs.record_event(
        db, business=business, kind="business_resumed" if body.visible else "business_paused",
        audience="owner" if _is_admin(current_user) else "admins", actor=_actor(current_user),
    )
    return await _full_out(db, business)


@router.delete("/businesses/{business_id}", status_code=204)
async def owner_delete_business(
    business_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Only an owner member can delete the business. A worker cannot."""
    business = await _business_or_404(db, business_id)
    member = await db.business_members.find_one({"business_id": str(business["_id"]), "user_id": current_user["uid"]})
    if not member or member.get("role") != "owner":
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    await _delete_business(db, business)


@router.delete("/admin/businesses/{business_id}", status_code=204)
async def admin_delete_business(
    business_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Ragly admins can remove a business, its branches, members, and invitations."""
    _require_admin(current_user)
    await _delete_business(db, await _business_or_404(db, business_id))


# ---------------------------------------------------------------------------
# Logo and gallery
# ---------------------------------------------------------------------------


async def _read_image(file: UploadFile) -> tuple[bytes, str]:
    content = await file.read(_MAX_PHOTO_BYTES + 1)
    identified = identify_image(content) if content and len(content) <= _MAX_PHOTO_BYTES else None
    if identified is None:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    return content, identified[0]


def _upload(business_id: str, content: bytes, content_type: str) -> str:
    try:
        return upload_business_image(business_id, content, content_type)
    except Exception:
        logger.exception("Business photo upload failed for %s", business_id)
        raise_api_error(500, ErrorCode.FAILED_TO_SAVE)


def _forget_image(business_id: str, url: str | None) -> None:
    if not url or not url.startswith("https://"):
        return
    try:
        delete_business_image(business_id, url)
    except Exception:
        logger.exception("Business photo delete failed for %s", business_id)


async def _set_logo(db: AsyncIOMotorDatabase, business: dict, file: UploadFile) -> BusinessOut:
    content, content_type = await _read_image(file)
    url = _upload(str(business["_id"]), content, content_type)
    previous = docs.logo(business)
    now = datetime.now(timezone.utc)
    await db.businesses.update_one(
        {"_id": business["_id"]},
        {"$set": {"photo": url, "updated_at": now}, "$unset": {"photo_url": ""}},
    )
    business.pop("photo_url", None)
    business["photo"] = url
    business["updated_at"] = now
    if previous and previous != url and previous not in docs.gallery(business):
        _forget_image(str(business["_id"]), previous)
    return await _full_out(db, business)


async def _clear_logo(db: AsyncIOMotorDatabase, business: dict) -> BusinessOut:
    previous = docs.logo(business)
    now = datetime.now(timezone.utc)
    await db.businesses.update_one(
        {"_id": business["_id"]},
        {"$set": {"photo": None, "updated_at": now}, "$unset": {"photo_url": ""}},
    )
    business.pop("photo_url", None)
    business["photo"] = None
    business["updated_at"] = now
    if previous and previous not in docs.gallery(business):
        _forget_image(str(business["_id"]), previous)
    return await _full_out(db, business)


async def _add_business_photo(db: AsyncIOMotorDatabase, business: dict, file: UploadFile) -> BusinessOut:
    content, content_type = await _read_image(file)
    photos = docs.gallery(business)
    if len(photos) >= docs.MAX_PHOTOS:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    url = _upload(str(business["_id"]), content, content_type)
    photos.append(url)
    now = datetime.now(timezone.utc)
    await db.businesses.update_one({"_id": business["_id"]}, {"$set": {"photos": photos, "updated_at": now}})
    business["photos"] = photos
    business["updated_at"] = now
    return await _full_out(db, business)


async def _remove_business_photo(db: AsyncIOMotorDatabase, business: dict, url: str) -> BusinessOut:
    url = url.strip()
    photos = docs.gallery(business)
    photo = docs.logo(business)
    if url not in photos and photo != url:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    photos = [item for item in photos if item != url]
    now = datetime.now(timezone.utc)
    update: dict = {"photos": photos, "updated_at": now}
    if photo == url:
        update["photo"] = None
        business["photo"] = None
        business.pop("photo_url", None)
    await db.businesses.update_one({"_id": business["_id"]}, {"$set": update, "$unset": {"photo_url": ""}})
    business["photos"] = photos
    business["updated_at"] = now
    _forget_image(str(business["_id"]), url)
    return await _full_out(db, business)


@router.post("/businesses/{business_id}/logo", response_model=BusinessOut)
async def owner_set_logo(
    business_id: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """The business logo shown on the dashboard and as the listing image."""
    business = await _business_or_404(db, business_id)
    await _require_brand_owner(db, business, current_user)
    return await _set_logo(db, business, file)


@router.delete("/businesses/{business_id}/logo", response_model=BusinessOut)
async def owner_clear_logo(
    business_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    business = await _business_or_404(db, business_id)
    await _require_brand_owner(db, business, current_user)
    return await _clear_logo(db, business)


@router.post("/admin/businesses/{business_id}/logo", response_model=BusinessOut)
async def admin_set_logo(
    business_id: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Admins can set the listing logo without being the owner."""
    _require_admin(current_user)
    business = await _business_or_404(db, business_id)
    return await _set_logo(db, business, file)


@router.delete("/admin/businesses/{business_id}/logo", response_model=BusinessOut)
async def admin_clear_logo(
    business_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    _require_admin(current_user)
    business = await _business_or_404(db, business_id)
    return await _clear_logo(db, business)


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


# ---------------------------------------------------------------------------
# Branches (owner and admin)
# ---------------------------------------------------------------------------


def _pin_timezone(body: BranchWrite) -> str | None:
    if body.location is None:
        return None
    longitude, latitude = body.location.coordinates
    return timezone_at(latitude, longitude)


def _build_branch_or_400(business: dict, body: BranchWrite, **kwargs) -> dict:
    try:
        return docs.build_branch(business, body, timezone_name=_pin_timezone(body), **kwargs)
    except docs.HoursError:
        raise_api_error(400, ErrorCode.BRANCH_HOURS_INVALID)


def _require_complete(branch: dict, business: dict) -> None:
    missing = docs.branch_missing(branch, business)
    if missing:
        raise_api_error_with(400, ErrorCode.BRANCH_INCOMPLETE, {"fields": missing})


async def _branch_editor(db: AsyncIOMotorDatabase, business: dict, location_id: str, current_user: dict) -> None:
    """Brand owners and admins edit any branch; a store manager edits their own."""
    if await _brand_owner(db, business, current_user):
        return
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


@router.post("/businesses/{business_id}/locations", response_model=LocationOut, status_code=201)
async def add_branch(
    business_id: str,
    body: BranchWrite,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Save a branch. For owners it is a draft until they send it; admins publish at once."""
    business = await _business_or_404(db, business_id)
    await _require_brand_owner(db, business, current_user)
    admin = _is_admin(current_user)
    rows = await docs.list_branches(db, str(business["_id"]))
    now = datetime.now(timezone.utc)
    doc = _build_branch_or_400(
        business, body,
        status="published" if admin else "draft",
        source="admin" if admin else "owner",
        now=now, is_main=not rows,
    )
    if admin:
        _require_complete(doc, business)
        doc["reviewed_at"] = now
        doc["reviewed_by"] = (current_user.get("email") or "").strip().lower()
    result = await db.business_locations.insert_one(doc)
    doc["_id"] = result.inserted_id
    await docs.sync_business(db, business)
    await docs.record_event(
        db, business=business, branch=doc,
        kind="branch_published" if admin else "branch_created",
        audience="owner" if admin else "admins", actor=_actor(current_user),
    )
    return docs.branch_out(doc, business)


@router.patch("/businesses/{business_id}/locations/{location_id}", response_model=LocationOut)
async def update_branch(
    business_id: str,
    location_id: str,
    body: BranchWrite,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Edit one branch. A live branch keeps its address and pin; a new service on it waits for review."""
    business = await _business_or_404(db, business_id)
    row = await _branch_or_404(db, business, location_id)
    await _branch_editor(db, business, location_id, current_user)
    admin = _is_admin(current_user)
    status = docs.branch_status(row)
    if status == "published" and not admin:
        same_address = body.address.strip() == docs.text(row.get("address"))
        same_point = (body.location.model_dump() if body.location else None) == docs.point_doc(row.get("location"))
        if not same_address or not same_point:
            raise_api_error(409, ErrorCode.BRANCH_NOT_EDITABLE)
    now = datetime.now(timezone.utc)
    doc = _build_branch_or_400(
        business, body, status=status, source="admin" if admin else "owner",
        now=now, is_main=bool(row.get("is_main")), existing=row,
    )
    if admin:
        for service in doc["services"]:
            if service["status"] == "pending_review":
                service["status"] = status
        doc["opening_hours"] = doc["services"][0]["opening_hours"] if doc["services"] else doc["opening_hours"]
    if status == "published":
        _require_complete(doc, business)
    await db.business_locations.update_one({"_id": row["_id"]}, {"$set": {k: v for k, v in doc.items() if k != "_id"}})
    await docs.sync_business(db, business)
    pending_services = [service["category"] for service in doc["services"] if service["status"] == "pending_review"]
    await docs.record_event(
        db, business=business, branch=doc, kind="branch_updated",
        audience="owner" if admin else "admins", actor=_actor(current_user),
        message=", ".join(pending_services) or None,
    )
    if pending_services and status == "published" and not admin:
        owner_email = (current_user.get("email") or "").strip().lower()
        _notify_admins(business, doc, owner_email)
    return docs.branch_out(doc, business)


@router.post("/businesses/{business_id}/locations/{location_id}/send", response_model=LocationOut)
async def send_branch(
    business_id: str,
    location_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Send a draft or returned branch for review. Admins hear about it; the owner gets a receipt."""
    business = await _business_or_404(db, business_id)
    row = await _branch_or_404(db, business, location_id)
    await _require_brand_owner(db, business, current_user)
    status = docs.branch_status(row)
    if status not in {"draft", "rejected"}:
        raise_api_error(400, ErrorCode.BRANCH_NOT_PENDING)
    _require_complete(row, business)
    now = datetime.now(timezone.utc)
    services = docs.services_of(row, business)
    for service in services:
        if service["status"] != "published":
            service["status"] = "pending_review"
            service["updated_at"] = now
    update = {
        "status": "pending_review",
        "services": services,
        "field_errors": {},
        "rejection_reason": None,
        "submitted_at": now,
        "updated_at": now,
    }
    await db.business_locations.update_one({"_id": row["_id"]}, {"$set": update})
    row.update(update)
    await docs.sync_business(db, business)
    owner_email = (current_user.get("email") or "").strip().lower()
    await docs.record_event(db, business=business, branch=row, kind="branch_sent", audience="admins", actor=_actor(current_user))
    _notify_admins(business, row, owner_email)
    email, locale = await _owner_contact(db, business)
    send_owner_update(
        email or owner_email, kind="received", locale=locale,
        business_name=docs.text(business.get("name")), address=docs.text(row.get("address")),
    )
    return docs.branch_out(row, business)


@router.post("/businesses/{business_id}/locations/{location_id}/withdraw", response_model=LocationOut)
async def withdraw_branch(
    business_id: str,
    location_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Take a waiting branch back to draft to keep editing it."""
    business = await _business_or_404(db, business_id)
    row = await _branch_or_404(db, business, location_id)
    await _require_brand_owner(db, business, current_user)
    if docs.branch_status(row) != "pending_review":
        raise_api_error(400, ErrorCode.BRANCH_NOT_PENDING)
    now = datetime.now(timezone.utc)
    services = docs.services_of(row, business)
    for service in services:
        if service["status"] == "pending_review":
            service["status"] = "draft"
            service["updated_at"] = now
    update = {"status": "draft", "services": services, "updated_at": now}
    await db.business_locations.update_one({"_id": row["_id"]}, {"$set": update})
    row.update(update)
    await docs.sync_business(db, business)
    await docs.record_event(db, business=business, branch=row, kind="branch_withdrawn", audience="admins", actor=_actor(current_user))
    return docs.branch_out(row, business)


@router.patch("/businesses/{business_id}/locations/{location_id}/visibility", response_model=LocationOut)
async def set_branch_visibility(
    business_id: str,
    location_id: str,
    body: BranchVisibility,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Pause or resume a live branch in the app without deleting it."""
    business = await _business_or_404(db, business_id)
    row = await _branch_or_404(db, business, location_id)
    await _require_brand_owner(db, business, current_user)
    admin = _is_admin(current_user)
    if not admin and row.get("hidden_by") == "admin" and body.visible:
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    now = datetime.now(timezone.utc)
    update = {
        "visible": body.visible,
        "hidden_reason": None if body.visible else docs.optional_text(body.reason),
        "hidden_by": None if body.visible else ("admin" if admin else "owner"),
        "updated_at": now,
    }
    await db.business_locations.update_one({"_id": row["_id"]}, {"$set": update})
    row.update(update)
    await docs.sync_business(db, business)
    await docs.record_event(
        db, business=business, branch=row, kind="branch_resumed" if body.visible else "branch_paused",
        audience="owner" if admin else "admins", actor=_actor(current_user), message=update["hidden_reason"],
    )
    return docs.branch_out(row, business)


@router.delete("/businesses/{business_id}/locations/{location_id}", status_code=204)
async def delete_branch(
    business_id: str,
    location_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Remove a branch. If it was the main one, the next branch becomes main."""
    business = await _business_or_404(db, business_id)
    row = await _branch_or_404(db, business, location_id)
    await _require_brand_owner(db, business, current_user)
    await db.business_locations.delete_one({"_id": row["_id"]})
    _forget_image(str(business["_id"]), docs.clean_photo(row.get("photo")))
    await docs.sync_business(db, business)
    await docs.record_event(db, business=business, branch=row, kind="branch_deleted", audience="admins", actor=_actor(current_user))
    return None


@router.post("/businesses/{business_id}/locations/{location_id}/photo", response_model=LocationOut)
async def set_branch_photo(
    business_id: str,
    location_id: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """One photo of the branch itself."""
    business = await _business_or_404(db, business_id)
    row = await _branch_or_404(db, business, location_id)
    await _branch_editor(db, business, location_id, current_user)
    content, content_type = await _read_image(file)
    url = _upload(str(business["_id"]), content, content_type)
    previous = docs.clean_photo(row.get("photo"))
    now = datetime.now(timezone.utc)
    await db.business_locations.update_one({"_id": row["_id"]}, {"$set": {"photo": url, "updated_at": now}})
    row["photo"] = url
    row["updated_at"] = now
    if not docs.profile_image(business):
        await db.businesses.update_one({"_id": business["_id"]}, {"$set": {"photo": url, "updated_at": now}})
        business["photo"] = url
    if previous and previous != url:
        _forget_image(str(business["_id"]), previous)
    return docs.branch_out(row, business)


@router.delete("/businesses/{business_id}/locations/{location_id}/photo", response_model=LocationOut)
async def clear_branch_photo(
    business_id: str,
    location_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    business = await _business_or_404(db, business_id)
    row = await _branch_or_404(db, business, location_id)
    await _branch_editor(db, business, location_id, current_user)
    previous = docs.clean_photo(row.get("photo"))
    now = datetime.now(timezone.utc)
    await db.business_locations.update_one({"_id": row["_id"]}, {"$set": {"photo": None, "updated_at": now}})
    row["photo"] = None
    row["updated_at"] = now
    _forget_image(str(business["_id"]), previous)
    return docs.branch_out(row, business)


@router.get("/businesses/{business_id}/events", response_model=list[EventOut])
async def business_events(
    business_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """What happened to this business, newest first. Admins see everything; owners see their side."""
    business = await _business_or_404(db, business_id)
    await _require_brand_owner(db, business, current_user)
    query: dict = {"business_id": str(business["_id"])}
    if not _is_admin(current_user):
        query["audience"] = "owner"
    rows = await db.business_events.find(query).sort([("created_at", -1), ("_id", -1)]).limit(100).to_list(100)
    return [docs.event_out(row) for row in rows]


# ---------------------------------------------------------------------------
# Admin: review queue
# ---------------------------------------------------------------------------


def _waiting(branch: dict, business: dict) -> bool:
    if docs.branch_status(branch) == "pending_review":
        return True
    return docs.branch_status(branch) == "published" and any(
        service["status"] == "pending_review" for service in docs.services_of(branch, business)
    )


async def _conflicts(db: AsyncIOMotorDatabase, branch: dict, business_id: str) -> list[PlaceConflict]:
    """Other businesses' branches at the same place: same OSM ref or a few metres away."""
    pair = docs.lat_lng(docs.point_doc(branch.get("location")))
    ref = docs.place_ref_doc(branch.get("place_ref"))
    if pair is None and ref is None:
        return []
    rows = await db.business_locations.find({"business_id": {"$ne": business_id}}).to_list(None)
    found: list[tuple[dict, int, bool]] = []
    for row in rows:
        other_ref = docs.place_ref_doc(row.get("place_ref"))
        same_ref = bool(ref and other_ref and other_ref == ref)
        other_pair = docs.lat_lng(docs.point_doc(row.get("location")))
        metres = int(round(_distance_km(pair[0], pair[1], other_pair[0], other_pair[1]) * 1000)) if pair and other_pair else 10**9
        if same_ref or metres <= _CONFLICT_METERS:
            found.append((row, metres if metres < 10**9 else 0, same_ref))
    if not found:
        return []
    ids = {ObjectId(row["business_id"]) for row, _, _ in found if ObjectId.is_valid(str(row.get("business_id")))}
    names = {str(doc["_id"]): docs.text(doc.get("name")) for doc in await db.businesses.find({"_id": {"$in": list(ids)}}).to_list(None)}
    return [
        PlaceConflict(
            business_id=str(row.get("business_id")),
            business_name=names.get(str(row.get("business_id")), ""),
            address=docs.text(row.get("address")),
            distance_m=metres,
            same_place_ref=same_ref,
        )
        for row, metres, same_ref in found
    ]


@router.get("/admin/review", response_model=list[ReviewGroup])
async def review_queue(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Everything that waits, grouped by business, oldest request first."""
    _require_admin(current_user)
    rows = await db.business_locations.find(
        {"$or": [{"status": "pending_review"}, {"services.status": "pending_review"}]}
    ).to_list(None)
    by_business: dict[str, list[dict]] = {}
    for row in rows:
        by_business.setdefault(str(row.get("business_id")), []).append(row)
    ids = [ObjectId(key) for key in by_business if ObjectId.is_valid(key)]
    businesses = {str(doc["_id"]): doc for doc in await db.businesses.find({"_id": {"$in": ids}}).to_list(None)} if ids else {}
    members = await db.business_members.find({"business_id": {"$in": list(by_business)}, "role": "owner"}).to_list(None) if by_business else []
    owners: dict[str, dict] = {}
    for member in members:
        owners.setdefault(str(member.get("business_id")), member)
    groups: list[ReviewGroup] = []
    for business_id, branches in by_business.items():
        business = businesses.get(business_id)
        if not business:
            continue
        waiting = [row for row in branches if _waiting(row, business)]
        if not waiting:
            continue
        waiting.sort(key=lambda row: (row.get("submitted_at") or row.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc)))
        owner = owners.get(business_id) or {}
        groups.append(
            ReviewGroup(
                business=await _full_out(db, business),
                branches=[ReviewBranch(branch=docs.branch_out(row, business), conflicts=await _conflicts(db, row, business_id)) for row in waiting],
                first_request=docs.business_status(business) != "published",
                owner_email=docs.optional_text(owner.get("email")) or docs.optional_text(business.get("owner_email")),
                owner_since=docs.utc(owner.get("created_at")),
                waiting_since=docs.utc(waiting[0].get("submitted_at") or waiting[0].get("updated_at")),
            )
        )
    groups.sort(key=lambda group: (group.waiting_since or datetime.max.replace(tzinfo=timezone.utc)))
    return groups


@router.get("/admin/locations", response_model=list[LocationOut])
async def admin_branches(
    status: Literal["draft", "pending_review", "published", "rejected"] = "pending_review",
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Branches by status, oldest first."""
    _require_admin(current_user)
    rows = await db.business_locations.find({"status": status}).sort("_id", 1).to_list(None)
    ids = [ObjectId(row["business_id"]) for row in rows if ObjectId.is_valid(str(row.get("business_id")))]
    businesses = {str(doc["_id"]): doc for doc in await db.businesses.find({"_id": {"$in": ids}}).to_list(None)} if ids else {}
    return [docs.branch_out(row, businesses.get(str(row.get("business_id")))) for row in rows]


async def _approve_branch(
    db: AsyncIOMotorDatabase,
    business: dict,
    row: dict,
    notes: dict[str, str],
    admin_email: str,
) -> dict:
    now = datetime.now(timezone.utc)
    was_published = docs.business_status(business) == "published"
    services = docs.services_of(row, business)
    for service in services:
        if service["status"] != "published":
            service["status"] = "published"
            service["updated_at"] = now
    update = {
        "status": "published",
        "visible": True if row.get("hidden_by") != "owner" else docs.branch_visible(row),
        "services": services,
        "opening_hours": services[0]["opening_hours"] if services else docs.empty_hours(provided=False),
        "field_errors": {},
        "admin_notes": notes,
        "rejection_reason": None,
        "reviewed_at": now,
        "reviewed_by": admin_email,
        "updated_at": now,
    }
    await db.business_locations.update_one({"_id": row["_id"]}, {"$set": update})
    row.update(update)
    business_update = {"admin_notes": {**docs.notes(business.get("admin_notes")), **notes}, "reviewed_by": admin_email}
    await db.businesses.update_one({"_id": business["_id"]}, {"$set": business_update})
    business.update(business_update)
    await docs.sync_business(db, business)
    owner_uid = docs.optional_text(business.get("owner_uid"))
    if owner_uid:
        await _upsert_owner_member(db, business, owner_uid, docs.optional_text(business.get("owner_email")), now)
    kind = "branch_approved" if was_published else "business_published"
    await docs.record_event(db, business=business, branch=row, kind=kind, audience="owner", actor={"email": admin_email})
    email, locale = await _owner_contact(db, business)
    send_owner_update(
        email, kind="branch_published" if was_published else "published", locale=locale,
        business_name=docs.text(business.get("name")), address=docs.text(row.get("address")), field_notes=notes,
    )
    return row


async def _reject_branch(
    db: AsyncIOMotorDatabase,
    business: dict,
    row: dict,
    field_errors: dict[str, str],
    admin_email: str,
) -> dict:
    now = datetime.now(timezone.utc)
    status = docs.branch_status(row)
    services = docs.services_of(row, business)
    for service in services:
        if service["status"] == "pending_review":
            service["status"] = "rejected"
            service["updated_at"] = now
    summary = "\n".join(note_lines(field_errors, "en"))
    update = {
        "status": "rejected" if status != "published" else "published",
        "services": services,
        "field_errors": field_errors,
        "admin_notes": field_errors,
        "rejection_reason": summary,
        "reviewed_at": now,
        "reviewed_by": admin_email,
        "updated_at": now,
    }
    await db.business_locations.update_one({"_id": row["_id"]}, {"$set": update})
    row.update(update)
    await docs.sync_business(db, business)
    if docs.business_status(business) == "rejected":
        business_update = {"field_errors": field_errors, "admin_notes": field_errors, "rejection_reason": summary, "reviewed_at": now, "reviewed_by": admin_email}
        await db.businesses.update_one({"_id": business["_id"]}, {"$set": business_update})
        business.update(business_update)
    await docs.record_event(db, business=business, branch=row, kind="branch_rejected", audience="owner", actor={"email": admin_email}, message=summary)
    email, locale = await _owner_contact(db, business)
    send_owner_update(
        email, kind="rejected", locale=locale,
        business_name=docs.text(business.get("name")), address=docs.text(row.get("address")), field_notes=field_errors,
    )
    return row


async def _waiting_branch(db: AsyncIOMotorDatabase, location_id: str) -> tuple[dict, dict]:
    if not ObjectId.is_valid(location_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    row = await db.business_locations.find_one({"_id": ObjectId(location_id)})
    if not row:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    business = await _business_or_404(db, str(row.get("business_id")))
    if not _waiting(row, business):
        raise_api_error(400, ErrorCode.BRANCH_NOT_PENDING)
    return row, business


@router.post("/admin/locations/{location_id}/approve", response_model=LocationOut)
async def approve_branch(
    location_id: str,
    body: BranchApprove | None = None,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Publish a branch. The first approved branch publishes the business and seats its owner."""
    admin_email = _require_admin(current_user)
    row, business = await _waiting_branch(db, location_id)
    _require_complete(row, business)
    row = await _approve_branch(db, business, row, body.field_notes if body else {}, admin_email)
    return docs.branch_out(row, business)


@router.post("/admin/locations/{location_id}/reject", response_model=LocationOut)
async def reject_branch(
    location_id: str,
    body: BranchReject,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Send a branch back with a note on each field to fix."""
    admin_email = _require_admin(current_user)
    notes = body.notes()
    if not notes:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    row, business = await _waiting_branch(db, location_id)
    row = await _reject_branch(db, business, row, notes, admin_email)
    return docs.branch_out(row, business)


@router.post("/admin/businesses/{business_id}/approve", response_model=BusinessOut)
async def approve_business(
    business_id: str,
    body: AdminApprove | None = None,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Approve every waiting branch of a business at once."""
    admin_email = _require_admin(current_user)
    business = await _business_or_404(db, business_id)
    rows = [row for row in await docs.list_branches(db, business_id) if _waiting(row, business)]
    if not rows:
        raise_api_error(400, ErrorCode.BUSINESS_NOT_PENDING)
    for row in rows:
        _require_complete(row, business)
    notes = body.field_notes if body else {}
    for row in rows:
        await _approve_branch(db, business, row, notes, admin_email)
    return await _full_out(db, business)


@router.post("/admin/businesses/{business_id}/reject", response_model=BusinessOut)
async def reject_business(
    business_id: str,
    body: BusinessReject,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Send every waiting branch of a business back with the same notes."""
    admin_email = _require_admin(current_user)
    business = await _business_or_404(db, business_id)
    rows = [row for row in await docs.list_branches(db, business_id) if _waiting(row, business)]
    if not rows:
        raise_api_error(400, ErrorCode.BUSINESS_NOT_PENDING)
    for row in rows:
        await _reject_branch(db, business, row, body.field_errors, admin_email)
    return await _full_out(db, business)


# ---------------------------------------------------------------------------
# Admin: create, list, invite
# ---------------------------------------------------------------------------


@router.post("/admin/businesses", response_model=BusinessOut, status_code=201)
async def admin_create_business(
    body: AdminCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """The admin one-form: a published business with its first branch, and an owner invitation."""
    admin_email = _require_admin(current_user)
    await _ensure_unique_name(db, body.business.name)
    now = datetime.now(timezone.utc)
    business = docs.build_business(
        body.business, status="published", source="admin", now=now,
        owner_uid=None, owner_email=None, locale=body.business.locale or "en",
    )
    business["_id"] = ObjectId()
    business["submitted_at"] = now
    business["reviewed_at"] = now
    business["reviewed_by"] = admin_email
    branch = _build_branch_or_400(business, body.branch, status="published", source="admin", now=now, is_main=True)
    _require_complete(branch, business)
    branch["submitted_at"] = now
    branch["reviewed_at"] = now
    branch["reviewed_by"] = admin_email
    mirrored = docs.mirror_fields(business, [branch])
    business.update(mirrored)
    try:
        await db.businesses.insert_one(business)
    except DuplicateKeyError:
        raise_api_error(409, ErrorCode.BUSINESS_NAME_TAKEN)
    result = await db.business_locations.insert_one(branch)
    branch["_id"] = result.inserted_id
    await docs.record_event(db, business=business, branch=branch, kind="business_published", audience="owner", actor={"email": admin_email})
    out = await _full_out(db, business)
    if body.owner_email:
        invite = await _create_invitation(db, business, body.owner_email, "owner", admin_email)
        out.invitations = [_invite_out(invite, business["name"])]
    return out


def _and_match(*parts: dict) -> dict:
    clauses: list[dict] = []
    for part in parts:
        if not part:
            continue
        if "$and" in part:
            clauses.extend(part["$and"])
        else:
            clauses.append(part)
    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


async def _directory_facets(
    db: AsyncIOMotorDatabase,
    status: str | None,
    category: str | None,
    q: str | None,
    ownership: str,
    owner_oids: list[ObjectId],
) -> DirectoryFacets:
    status_match = _page_match(None, category, q, ownership, owner_oids)
    category_match = _page_match(status, None, q, ownership, owner_oids)
    status_rows = await db.businesses.aggregate(
        [
            *([{"$match": status_match}] if status_match else []),
            {"$group": {"_id": "$status", "n": {"$sum": 1}}},
        ]
    ).to_list(None)
    by_status = {str(row["_id"]): int(row["n"]) for row in status_rows if row.get("_id")}
    by_category = {"all": await db.businesses.count_documents(category_match)}
    for name in CATEGORIES:
        by_category[name] = await db.businesses.count_documents(
            _and_match(category_match, {"$or": [{"categories": name}, {"category": name}]})
        )
    owned = await db.businesses.count_documents(_page_match(status, category, q, "owned", owner_oids))
    unowned = await db.businesses.count_documents(_page_match(status, category, q, "unowned", owner_oids))
    ownership_all = await db.businesses.count_documents(_page_match(status, category, q, "all", owner_oids))
    return DirectoryFacets(
        status={"all": sum(by_status.values()), **by_status},
        category=by_category,
        ownership={"all": ownership_all, "owned": owned, "unowned": unowned},
    )


def _page_match(status: str | None, category: str | None, q: str | None, ownership: str, owner_oids: list[ObjectId]) -> dict:
    clauses: list[dict] = []
    if status == "reviewed":
        clauses.append({"status": {"$in": ["published", "rejected"]}})
    elif status:
        clauses.append({"status": status})
    if category:
        clauses.append({"$or": [{"categories": category}, {"category": category}]})
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
        clauses.append({"$or": [{"owner_uid": {"$type": "string"}}, {"_id": {"$in": owner_oids}}]})
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
    """Counts for the admin dashboard. Pending counts businesses with something waiting."""
    _require_admin(current_user)
    counts = {"pending_review": 0, "published": 0, "rejected": 0}
    rows = await db.businesses.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]).to_list(None)
    for row in rows:
        if row.get("_id") in counts:
            counts[row["_id"]] = row["n"]
    waiting_rows = await db.business_locations.find(
        {"$or": [{"status": "pending_review"}, {"services.status": "pending_review"}]}, {"business_id": 1}
    ).to_list(None)
    waiting = {str(row.get("business_id")) for row in waiting_rows}
    deleted = await db.business_deletions.count_documents({})
    total = await db.businesses.count_documents({})
    return BusinessCounts(
        total=total,
        pending=max(counts["pending_review"], len(waiting)),
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
    """One page of businesses. The next page is requested as the list scrolls."""
    _require_admin(current_user)
    allowed_status = {"draft", "pending_review", "published", "rejected", "suspended", "reviewed"}
    if status and status not in allowed_status:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    if category and category not in CATEGORIES:
        raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
    owner_oids: list[ObjectId] = []
    member_rows = await db.business_members.find({"role": "owner"}, {"business_id": 1}).to_list(None)
    for row in member_rows:
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
                                {"case": {"$eq": ["$category", name]}, "then": index}
                                for index, name in enumerate(CATEGORIES)
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
        rows = await db.businesses.aggregate(pipeline).to_list(None)
    else:
        rows = await db.businesses.find(match).sort(sort_spec).skip(skip).limit(limit + 1).to_list(None)
    has_more = len(rows) > limit
    total = await db.businesses.count_documents(match)
    facets = await _directory_facets(db, status, category, q, ownership, owner_oids)
    return BusinessPage(items=await _decorate(db, rows[:limit]), has_more=has_more, total=total, facets=facets)


@router.get("/admin/businesses", response_model=list[BusinessOut])
async def list_businesses_for_review(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Every business, pending first. Admins only."""
    _require_admin(current_user)
    rows = await db.businesses.find({}).sort("submitted_at", -1).to_list(None)
    order = {"pending_review": 0, "rejected": 1, "draft": 2, "published": 3, "suspended": 4}
    rows.sort(key=lambda doc: (order.get(doc.get("status"), 9),))
    return await _decorate(db, rows)


@router.get("/admin/businesses/{business_id}", response_model=BusinessOut)
async def admin_get_business(
    business_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """One business with its branches, for the admin branch editor."""
    _require_admin(current_user)
    business = await _business_or_404(db, business_id)
    return await _full_out(db, business)


@router.get("/admin/businesses/{business_id}/reviews", response_model=list[PlaceReview])
async def admin_list_reviews(
    business_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Every review on this business, including unpublished listings."""
    _require_admin(current_user)
    await _business_or_404(db, business_id)
    return await _place_reviews(db, business_id, current_user["uid"], limit=200)


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


# ---------------------------------------------------------------------------
# Team invitations
# ---------------------------------------------------------------------------


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
            rows = await docs.ensure_branches(db, business)
            location_id = str(rows[0]["_id"]) if rows else None
        if not location_id or not ObjectId.is_valid(location_id):
            raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
        branch = await db.business_locations.find_one({"_id": ObjectId(location_id), "business_id": str(business["_id"])})
        if not branch:
            raise_api_error(404, ErrorCode.NOT_FOUND)
        if docs.branch_status(branch) != "published":
            raise_api_error(400, ErrorCode.BUSINESS_INCOMPLETE)
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
        db, business, body.email, body.role,
        (current_user.get("email") or "").strip().lower(), location_id, reports_to,
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


async def _respond_invitation(invitation_id: str, current_user: dict, db: AsyncIOMotorDatabase, *, accepted: bool) -> InvitationOut:
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
        {"_id": invite["_id"]}, {"$set": {"status": status, "updated_at": now, "responded_at": now}}
    )
    invite["status"] = status
    if accepted:
        uid = current_user["uid"]
        role = invite.get("role") if invite.get("role") in _ROLES else "worker"
        if role == "owner":
            await _upsert_owner_member(db, business, uid, email, now)
            if not business.get("owner_uid"):
                person = await db.users.find_one({"firebase_uid": uid}) or {}
                locale = docs.text(person.get("locale")).lower()[:2]
                update = {"owner_uid": uid, "owner_email": email, "updated_at": now}
                if locale in {"en", "ro", "ru", "he"} and not docs.text(business.get("locale")):
                    update["locale"] = locale
                await db.businesses.update_one({"_id": business["_id"]}, {"$set": update})
        else:
            location_id = docs.optional_text(invite.get("location_id"))
            await db.business_members.update_one(
                {"business_id": str(business["_id"]), "user_id": uid, "location_id": location_id},
                {
                    "$set": {
                        "business_id": str(business["_id"]),
                        "user_id": uid,
                        "email": email,
                        "role": role,
                        "location_id": location_id,
                        "reports_to": docs.optional_text(invite.get("reports_to")),
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
        await docs.record_event(
            db, business=business, kind="owner_joined" if role == "owner" else "member_joined",
            audience="admins", actor=_actor(current_user), message=email,
        )
    return _invite_out(invite, business.get("name") or "")
