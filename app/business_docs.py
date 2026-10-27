"""One place that builds and reads business, branch, and service documents.

The owner website and the admin one-form both go through these builders, so a
business an admin inserted and a business an owner requested have the same
keys. Legacy documents (single address on the business, branch rows without
services) are read through the same normalizers and migrated on startup.

Document shapes
---------------
businesses
    name, name_key, phone[], email, description, website, instagram, photo,
    photos[], locale, status, owner_uid, owner_email, source,
    field_errors{}, admin_notes{}, rejection_reason,
    submitted_at, reviewed_at, reviewed_by, created_at, updated_at
    + mirrored from the main branch for older clients and admin filters:
    category, categories[], city, address, timezone, opening_hours, location

business_locations (branches)
    business_id, is_main, address, city, timezone, location, place_ref,
    services[], opening_hours (first service, mirrored), phone[], website,
    instagram, photo, status, visible, hidden_reason, hidden_by, source,
    field_errors{}, admin_notes{}, rejection_reason,
    submitted_at, reviewed_at, reviewed_by, created_at, updated_at

service (inside a branch)
    _id, category, opening_hours, phone[], status, visible, created_at, updated_at

business_events
    business_id, location_id, kind, actor_uid, actor_email, audience, message, created_at
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.business_names import business_name_key
from app.core.utils import doc_to_dict
from app.models.business import (
    CATEGORIES,
    BranchWrite,
    BusinessDetails,
    BusinessOut,
    EventOut,
    LocationOut,
    OnboardingStep,
    OpeningHours,
    PlaceLocation,
    PlaceService,
    ServiceOut,
)

logger = logging.getLogger("petto")

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_TIMEZONE = "Europe/Chisinau"
BRANCH_STATUSES = frozenset({"draft", "pending_review", "published", "rejected"})
BUSINESS_STATUSES = frozenset({"draft", "pending_review", "published", "rejected", "suspended"})
MAX_PHOTOS = 6
ONBOARDING_STEPS = ("description", "logo", "phones", "email", "website", "instagram", "done")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class HoursError(ValueError):
    """Opening hours that cannot be stored."""


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------


def text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def optional_text(value: object) -> str | None:
    return text(value) or None


def phones(raw: object) -> list[str]:
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def point_doc(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    coords = raw.get("coordinates")
    if not isinstance(coords, list) or len(coords) != 2:
        return None
    try:
        longitude, latitude = float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None
    return {"type": "Point", "coordinates": [longitude, latitude]}


def legacy_point(doc: dict) -> dict | None:
    """A GeoJSON point, or the old latitude/longitude pair."""
    point = point_doc(doc.get("location"))
    if point:
        return point
    latitude = doc.get("latitude")
    longitude = doc.get("longitude")
    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
        return point_doc({"coordinates": [longitude, latitude]})
    return None


def lat_lng(point: dict | None) -> tuple[float, float] | None:
    if not point:
        return None
    coords = point.get("coordinates")
    if not isinstance(coords, list) or len(coords) != 2:
        return None
    try:
        return float(coords[1]), float(coords[0])
    except (TypeError, ValueError):
        return None


def place_ref_doc(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    provider = text(raw.get("provider"))
    ref = text(raw.get("id"))
    if provider not in {"osm", "google"} or not ref:
        return None
    return {"provider": provider, "id": ref[:200]}


def notes(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        key: value.strip()
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }


def gallery(doc: dict) -> list[str]:
    raw = doc.get("photos")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item.startswith("https://")][:MAX_PHOTOS]


def logo(doc: dict) -> str | None:
    return optional_text(doc.get("photo")) or optional_text(doc.get("photo_url"))


def valid_timezone(name: object) -> str | None:
    value = text(name)
    if not value:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return value


def onboarding_step_of(doc: dict) -> OnboardingStep:
    """Resume the owner wizard. Legacy owner drafts that never finished come back at description."""
    raw = optional_text(doc.get("onboarding_step"))
    if raw in ONBOARDING_STEPS:
        return raw  # type: ignore[return-value]
    if text(doc.get("source")) == "owner" and business_status(doc) == "draft":
        return "description"
    return "done"


def _onboarding_step_for(details: BusinessDetails, previous: dict, source: str) -> OnboardingStep:
    if source == "admin" or text(previous.get("source")) == "admin":
        return "done"
    requested = details.onboarding_step
    if requested in ONBOARDING_STEPS:
        return requested
    stored = optional_text(previous.get("onboarding_step"))
    if stored in ONBOARDING_STEPS:
        return stored  # type: ignore[return-value]
    return "description" if not previous else onboarding_step_of(previous)


def clean_photo(value: object) -> str | None:
    """A stored photo is an https URL or a bundled asset file name."""
    photo = optional_text(value)
    if not photo:
        return None
    if "://" in photo:
        return photo if photo.startswith("https://") else None
    if "/" in photo or "\\" in photo or ".." in photo:
        return None
    return photo


def https_url(value: object) -> str | None:
    photo = clean_photo(value)
    if photo and photo.startswith("https://"):
        return photo
    return None


def profile_image(business: dict, branches: list[dict] | None = None) -> str | None:
    """The listing photo every branch shares: logo, then gallery, then a branch photo."""
    photo = https_url(logo(business))
    if photo:
        return photo
    photos = gallery(business)
    if photos:
        return photos[0]
    for branch in branches or []:
        photo = https_url(branch.get("photo"))
        if photo:
            return photo
    return None


def utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Opening hours
# ---------------------------------------------------------------------------


def empty_hours(*, provided: bool) -> dict:
    return {
        "provided": provided,
        "always_open": False,
        **{day: [] for day in DAYS},
        "special_dates": [],
    }


def minutes(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    if value == "24:00":
        return 24 * 60
    if not _TIME.match(value):
        return None
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _slot_list(raw: object) -> list[dict]:
    slots: list[dict] = []
    for slot in raw if isinstance(raw, list) else []:
        if not isinstance(slot, dict):
            continue
        start = minutes(slot.get("open"))
        end = minutes(slot.get("close"))
        if start is None or end is None or start >= 24 * 60 or end <= start:
            continue
        slots.append({"open": str(slot["open"]), "close": str(slot["close"])})
    slots.sort(key=lambda slot: slot["open"])
    return slots


def hours_doc(raw: object) -> dict:
    """Normalize stored hours, whatever version wrote them."""
    source = raw if isinstance(raw, dict) else {}
    provided = bool(source["provided"]) if "provided" in source else True
    if not provided:
        return empty_hours(provided=False)
    hours = empty_hours(provided=True)
    hours["always_open"] = bool(source.get("always_open", False))
    for day in DAYS:
        hours[day] = _slot_list(source.get(day))
    if hours["always_open"]:
        hours.update({day: [{"open": "00:00", "close": "24:00"}] for day in DAYS})
    special: list[dict] = []
    for item in source.get("special_dates") if isinstance(source.get("special_dates"), list) else []:
        if not isinstance(item, dict):
            continue
        day = text(item.get("date"))
        if not _DATE.match(day):
            continue
        closed = bool(item.get("closed", False))
        special.append({"date": day, "closed": closed, "slots": [] if closed else _slot_list(item.get("slots"))})
    special.sort(key=lambda item: item["date"])
    hours["special_dates"] = special
    return hours


def _check_slots(slots: list) -> list[dict]:
    stored: list[dict] = []
    for slot in slots:
        start = minutes(slot.open)
        end = minutes(slot.close)
        if start is None or end is None or start >= 24 * 60:
            raise HoursError("hours")
        if end <= start:
            raise HoursError("hours")
        stored.append({"open": slot.open, "close": slot.close})
    stored.sort(key=lambda slot: slot["open"])
    for first, second in zip(stored, stored[1:]):
        if minutes(second["open"]) < minutes(first["close"]):
            raise HoursError("hours")
    return stored


def store_hours(hours: OpeningHours) -> dict:
    """Validate hours from a form and return the document to store.

    `always_open` stores the flag and a full 00:00–24:00 on every day, so an app
    that reads only the day lists shows the same thing.
    """
    if not hours.provided:
        return empty_hours(provided=False)
    stored = empty_hours(provided=True)
    if hours.always_open:
        stored["always_open"] = True
        stored.update({day: [{"open": "00:00", "close": "24:00"}] for day in DAYS})
    else:
        for day in DAYS:
            stored[day] = _check_slots(getattr(hours, day))
    special: list[dict] = []
    seen: set[str] = set()
    for item in hours.special_dates:
        try:
            date.fromisoformat(item.date)
        except ValueError:
            raise HoursError("hours")
        if item.date in seen:
            raise HoursError("hours")
        seen.add(item.date)
        slots = [] if item.closed else _check_slots(item.slots)
        if not item.closed and not slots:
            raise HoursError("hours")
        special.append({"date": item.date, "closed": item.closed, "slots": slots})
    special.sort(key=lambda item: item["date"])
    stored["special_dates"] = special
    return stored


def hours_complete(hours: dict) -> bool:
    """Hours that can be published: none, always open, or at least one slot."""
    if not hours.get("provided", True):
        return True
    if hours.get("always_open"):
        return True
    return any(hours.get(day) for day in DAYS)


def _clock(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


def schedule(hours: dict, tz_name: str | None) -> dict:
    """Open or closed right now in the branch timezone, plus the next change."""
    closed = {
        "open_now": False,
        "open_24_7": False,
        "closes_at": None,
        "opens_at": None,
        "next_open_day": None,
        "opens_tomorrow": False,
    }
    if not hours.get("provided", True):
        return {**closed, "open_now": None}
    if hours.get("always_open"):
        return {**closed, "open_now": True, "open_24_7": True}
    try:
        zone = ZoneInfo(tz_name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    now = datetime.now(zone)
    today = now.weekday()
    now_min = now.hour * 60 + now.minute
    special = {item["date"]: item for item in hours.get("special_dates") or []}

    def parsed(slots: list) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []
        for slot in slots:
            start = minutes(slot.get("open"))
            end = minutes(slot.get("close"))
            if start is None or end is None or end <= start:
                continue
            result.append((start, end))
        return result

    def slots_for(offset: int) -> list[tuple[int, int]]:
        day = (now.date() + timedelta(days=offset)).isoformat()
        override = special.get(day)
        if override is not None:
            return [] if override.get("closed") else parsed(override.get("slots") or [])
        return parsed(hours.get(DAYS[(today + offset) % 7]) or [])

    def full_day(index: int) -> bool:
        return any(start == 0 and end >= 24 * 60 - 1 for start, end in parsed(hours.get(DAYS[index]) or []))

    if all(full_day(index) for index in range(7)) and not special:
        return {**closed, "open_now": True, "open_24_7": True}

    for start, end in slots_for(0):
        if start <= now_min < end:
            return {**closed, "open_now": True, "closes_at": _clock(end)}
    later = [start for start, _end in slots_for(0) if start > now_min]
    if later:
        return {**closed, "opens_at": _clock(min(later)), "opens_in_days": 0}
    for offset in range(1, 15):
        upcoming = slots_for(offset)
        if not upcoming:
            continue
        return {
            **closed,
            "opens_at": _clock(min(start for start, _end in upcoming)),
            "next_open_day": DAYS[(today + offset) % 7],
            "opens_tomorrow": offset == 1,
            "opens_in_days": offset,
        }
    return closed


def _public_hours(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "opens_in_days"}


def public_schedule(hours: dict, tz_name: str | None) -> dict:
    """Card/detail hours without internal ranking keys."""
    return _public_hours(schedule(hours, tz_name))


def _soonest_open(rows: list[dict]) -> dict:
    ranked = [row for row in rows if row.get("opens_at")]
    if not ranked:
        return rows[0]
    return min(
        ranked,
        key=lambda row: (row.get("opens_in_days", 99), row.get("opens_at") or "99:99"),
    )


def card_schedule(branch: dict, business: dict | None = None, *, category: str | None = None) -> dict:
    """Hours for a Near you card. Mixed live services become some_services_closed."""
    tz_name = branch_timezone(branch, business)
    if category:
        return {**_public_hours(schedule(branch_hours(branch, business, category=category), tz_name)), "some_services_closed": False}
    services = public_services(branch, business)
    if len(services) <= 1:
        hours = services[0]["opening_hours"] if services else branch_hours(branch, business)
        return {**_public_hours(schedule(hours, tz_name)), "some_services_closed": False}
    rows = [schedule(service["opening_hours"], tz_name) for service in services]
    known = [row for row in rows if row["open_now"] is not None]
    opened = [row for row in known if row["open_now"]]
    closed_rows = [row for row in known if row["open_now"] is False]
    if opened and closed_rows:
        return {**_public_hours(opened[0]), "some_services_closed": True}
    if opened:
        return {**_public_hours(opened[0]), "some_services_closed": False}
    if closed_rows:
        return {**_public_hours(_soonest_open(closed_rows)), "some_services_closed": False}
    return {**_public_hours(rows[0] if rows else schedule(empty_hours(provided=False), tz_name)), "some_services_closed": False}


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def build_service(
    category: str,
    hours: dict,
    phone: list[str],
    *,
    status: str,
    now: datetime,
    service_id: ObjectId | None = None,
    created_at: datetime | None = None,
    visible: bool = True,
) -> dict:
    return {
        "_id": service_id or ObjectId(),
        "category": category,
        "opening_hours": hours,
        "phone": phones(phone),
        "status": status if status in BRANCH_STATUSES else "draft",
        "visible": bool(visible),
        "created_at": created_at or now,
        "updated_at": now,
    }


def _service_doc(raw: object, fallback_status: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    category = text(raw.get("category"))
    if category not in CATEGORIES:
        return None
    raw_id = raw.get("_id", raw.get("id"))
    service_id = raw_id if isinstance(raw_id, ObjectId) else (
        ObjectId(raw_id) if isinstance(raw_id, str) and ObjectId.is_valid(raw_id) else ObjectId()
    )
    status = text(raw.get("status"))
    return {
        "_id": service_id,
        "category": category,
        "opening_hours": hours_doc(raw.get("opening_hours")),
        "phone": phones(raw.get("phone")),
        "status": status if status in BRANCH_STATUSES else fallback_status,
        "visible": bool(raw.get("visible", True)),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
    }


def services_of(branch: dict, business: dict | None = None) -> list[dict]:
    """The branch services. A row written before services existed yields one
    service from the business category and the branch hours."""
    status = branch_status(branch)
    raw = branch.get("services")
    if isinstance(raw, list):
        services = [service for service in (_service_doc(item, status) for item in raw) if service]
        if services or raw == []:
            return _dedupe_services(services)
    category = text((business or {}).get("category"))
    if category not in CATEGORIES:
        return []
    return [
        {
            "_id": _legacy_service_id(branch),
            "category": category,
            "opening_hours": hours_doc(branch.get("opening_hours")),
            "phone": [],
            "status": status,
            "visible": True,
            "created_at": branch.get("created_at"),
            "updated_at": branch.get("updated_at"),
        }
    ]


def _legacy_service_id(branch: dict) -> ObjectId:
    """A stable id for a service that is not stored yet: derived from the branch id."""
    raw = str(branch.get("_id") or "")
    if ObjectId.is_valid(raw):
        flipped = raw[:-1] + ("0" if raw[-1] != "0" else "1")
        return ObjectId(flipped)
    return ObjectId()


def _dedupe_services(services: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for service in services:
        if service["category"] in seen:
            continue
        seen.add(service["category"])
        unique.append(service)
    return unique


def public_services(branch: dict, business: dict | None = None) -> list[dict]:
    return [
        service
        for service in services_of(branch, business)
        if service["status"] == "published" and service["visible"]
    ]


def service_out(service: dict) -> ServiceOut:
    return ServiceOut(
        id=str(service["_id"]),
        category=service["category"],
        opening_hours=OpeningHours(**service["opening_hours"]),
        phone=service["phone"],
        status=service["status"],
        visible=service["visible"],
    )


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------


def branch_status(doc: dict) -> str:
    """Rows saved before review existed have no status. Those are live."""
    status = doc.get("status")
    if status in BRANCH_STATUSES:
        return str(status)
    return "published"


def business_visible(doc: dict) -> bool:
    return bool(doc.get("visible", True))


def branch_visible(doc: dict) -> bool:
    return bool(doc.get("visible", True))


def is_public_branch(doc: dict) -> bool:
    return branch_status(doc) == "published" and branch_visible(doc)


def public_branches(rows: list[dict]) -> list[dict]:
    return [row for row in rows if is_public_branch(row)]


def branch_hours(branch: dict, business: dict | None = None, *, category: str | None = None) -> dict:
    """The hours to show for a branch: the chosen service, else the first live one."""
    services = public_services(branch, business) or services_of(branch, business)
    if category:
        for service in services:
            if service["category"] == category:
                return service["opening_hours"]
    if services:
        return services[0]["opening_hours"]
    return hours_doc(branch.get("opening_hours"))


def branch_categories(branch: dict, business: dict | None = None, *, public_only: bool) -> list[str]:
    services = public_services(branch, business) if public_only else services_of(branch, business)
    ordered: list[str] = []
    for service in services:
        if service["category"] not in ordered:
            ordered.append(service["category"])
    return ordered


def branch_timezone(branch: dict, business: dict | None = None) -> str:
    return (
        valid_timezone(branch.get("timezone"))
        or valid_timezone((business or {}).get("timezone"))
        or DEFAULT_TIMEZONE
    )


def build_branch(
    business: dict,
    body: BranchWrite,
    *,
    status: str,
    source: str,
    now: datetime,
    is_main: bool,
    existing: dict | None = None,
    timezone_name: str | None = None,
) -> dict:
    """The full branch document from a form. Raises HoursError on bad hours.

    Existing services keep their ids and their published status; a new
    category on a live branch starts as pending_review until an admin
    approves it.
    """
    previous = existing or {}
    previous_services = {str(service["_id"]): service for service in services_of(previous, business)} if existing else {}
    services: list[dict] = []
    seen: set[str] = set()
    for item in body.services:
        if item.category in seen:
            continue
        seen.add(item.category)
        hours = store_hours(item.opening_hours)
        old = previous_services.get(item.id or "")
        if old is None:
            old = next((svc for svc in previous_services.values() if svc["category"] == item.category), None)
        if old is not None:
            service_status = old["status"] if old["status"] == "published" and status == "published" else status
            services.append(
                build_service(
                    item.category,
                    hours,
                    item.phone,
                    status=service_status,
                    now=now,
                    service_id=old["_id"],
                    created_at=old.get("created_at") or now,
                    visible=old.get("visible", True),
                )
            )
        else:
            new_status = "pending_review" if status == "published" and source == "owner" else status
            services.append(build_service(item.category, hours, item.phone, status=new_status, now=now))
    point = point_doc(body.location.model_dump()) if body.location else None
    zone = (
        valid_timezone(body.timezone)
        or valid_timezone(timezone_name)
        or valid_timezone(previous.get("timezone"))
        or valid_timezone(business.get("timezone"))
        or DEFAULT_TIMEZONE
    )
    created_at = previous.get("created_at") or now
    return {
        **({"_id": previous["_id"]} if previous.get("_id") is not None else {}),
        "business_id": str(business["_id"]),
        "is_main": bool(is_main),
        "address": body.address.strip(),
        "city": body.city.strip(),
        "timezone": zone,
        "location": point,
        "place_ref": body.place_ref.model_dump() if body.place_ref else None,
        "services": services,
        "opening_hours": services[0]["opening_hours"] if services else empty_hours(provided=False),
        "phone": body.phone if body.phone is not None else phones(previous.get("phone")),
        "website": body.website,
        "instagram": body.instagram,
        "photo": clean_photo(body.photo) if body.photo is not None else clean_photo(previous.get("photo")),
        "status": status,
        "visible": bool(previous.get("visible", True)),
        "hidden_reason": optional_text(previous.get("hidden_reason")),
        "hidden_by": previous.get("hidden_by") if previous.get("hidden_by") in {"owner", "admin"} else None,
        "source": source if not existing else (text(previous.get("source")) or source),
        "field_errors": notes(previous.get("field_errors")),
        "admin_notes": notes(previous.get("admin_notes")),
        "rejection_reason": optional_text(previous.get("rejection_reason")),
        "submitted_at": previous.get("submitted_at"),
        "reviewed_at": previous.get("reviewed_at"),
        "reviewed_by": optional_text(previous.get("reviewed_by")),
        "created_at": created_at,
        "updated_at": now,
    }


def branch_missing(branch: dict, business: dict | None = None) -> list[str]:
    """Fields a branch still needs before it can be sent for review."""
    missing: list[str] = []
    if not text(branch.get("address")):
        missing.append("address")
    if point_doc(branch.get("location")) is None:
        missing.append("location")
    services = services_of(branch, business)
    if not services:
        missing.append("services")
    elif not all(hours_complete(service["opening_hours"]) for service in services):
        missing.append("hours")
    # A listing card needs a picture: the business logo, or this branch's photo.
    if text((business or {}).get("source")) != "admin" and not logo(business or {}) and not clean_photo(branch.get("photo")):
        missing.append("photo")
    return missing


def branch_out(doc: dict, business: dict | None = None) -> LocationOut:
    data = doc_to_dict(doc)
    services = services_of(doc, business)
    return LocationOut(
        id=data["id"],
        business_id=str(doc.get("business_id") or ""),
        is_main=bool(doc.get("is_main", False)),
        address=text(doc.get("address")),
        city=text(doc.get("city")),
        timezone=branch_timezone(doc, business),
        location=point_doc(doc.get("location")),
        place_ref=place_ref_doc(doc.get("place_ref")),
        services=[service_out(service) for service in services],
        opening_hours=OpeningHours(**branch_hours(doc, business)),
        phone=phones(doc.get("phone")),
        website=optional_text(doc.get("website")),
        instagram=optional_text(doc.get("instagram")),
        photo=clean_photo(doc.get("photo")),
        status=branch_status(doc),
        visible=branch_visible(doc),
        hidden_reason=optional_text(doc.get("hidden_reason")),
        hidden_by=doc.get("hidden_by") if doc.get("hidden_by") in {"owner", "admin"} else None,
        rejection_reason=optional_text(doc.get("rejection_reason")),
        field_errors=notes(doc.get("field_errors")),
        admin_notes=notes(doc.get("admin_notes")),
        submitted_at=data.get("submitted_at"),
        reviewed_at=data.get("reviewed_at"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


def place_branch(
    doc: dict,
    business: dict,
    business_phone: list[str],
    distance_km: float | None,
    *,
    branches: list[dict] | None = None,
) -> PlaceLocation:
    """A branch on the public business screen."""
    branch_phone = phones(doc.get("phone"))
    services = public_services(doc, business)
    return PlaceLocation(
        id=str(doc.get("_id")),
        address=text(doc.get("address")),
        city=text(doc.get("city")),
        opening_hours=OpeningHours(**branch_hours(doc, business)),
        phone=branch_phone,
        call_phone=branch_phone or business_phone,
        shared_phone=not branch_phone and bool(business_phone),
        location=point_doc(doc.get("location")),
        distance_km=distance_km,
        categories=[service["category"] for service in services],
        services=[
            PlaceService(
                id=str(service["_id"]),
                category=service["category"],
                opening_hours=OpeningHours(**service["opening_hours"]),
                phone=service["phone"] or branch_phone or business_phone,
            )
            for service in services
        ],
        photo=profile_image(business, branches if branches is not None else [doc]),
    )


# ---------------------------------------------------------------------------
# Businesses
# ---------------------------------------------------------------------------


def business_status(doc: dict) -> str:
    status = doc.get("status")
    if status in BUSINESS_STATUSES:
        return str(status)
    return "published" if doc.get("published") is not False else "draft"


def business_locale(doc: dict) -> str:
    locale = text(doc.get("locale")).lower()
    return locale if locale in {"en", "ro", "ru", "he"} else "en"


def build_business(
    details: BusinessDetails,
    *,
    status: str,
    source: str,
    now: datetime,
    owner_uid: str | None,
    owner_email: str | None,
    locale: str,
    existing: dict | None = None,
) -> dict:
    """The full business document. Branch-mirrored fields are filled by sync_business."""
    previous = existing or {}
    name = details.name.strip()
    doc = {
        **({"_id": previous["_id"]} if previous.get("_id") is not None else {}),
        "name": name,
        "name_key": business_name_key(name),
        "phone": details.phone,
        "email": details.email,
        "description": optional_text(details.description),
        "website": details.website,
        "instagram": details.instagram,
        "photo": clean_photo(previous.get("photo")) or clean_photo(previous.get("photo_url")),
        "photos": gallery(previous),
        "locale": (details.locale or (business_locale(previous) if existing else None) or locale or "en"),
        "status": status if status in BUSINESS_STATUSES else "draft",
        "visible": bool(previous["visible"]) if "visible" in previous else True,
        "owner_uid": owner_uid if owner_uid is not None else optional_text(previous.get("owner_uid")),
        "owner_email": (owner_email or optional_text(previous.get("owner_email")) or None),
        "source": text(previous.get("source")) or source,
        "field_errors": notes(previous.get("field_errors")),
        "admin_notes": notes(previous.get("admin_notes")),
        "rejection_reason": optional_text(previous.get("rejection_reason")),
        "submitted_at": previous.get("submitted_at"),
        "reviewed_at": previous.get("reviewed_at"),
        "reviewed_by": optional_text(previous.get("reviewed_by")),
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
        "onboarding_step": _onboarding_step_for(details, previous, source),
        # Mirrored from the main branch. Empty until a branch exists.
        "category": previous.get("category") if previous.get("category") in CATEGORIES else None,
        "categories": [c for c in (previous.get("categories") or []) if c in CATEGORIES],
        "city": text(previous.get("city")),
        "address": text(previous.get("address")),
        "timezone": valid_timezone(previous.get("timezone")) or DEFAULT_TIMEZONE,
        "opening_hours": hours_doc(previous.get("opening_hours")) if previous.get("opening_hours") else empty_hours(provided=False),
        "location": legacy_point(previous),
    }
    return doc


def business_out(doc: dict) -> BusinessOut:
    data = doc_to_dict(doc)
    owner_uid = optional_text(data.get("owner_uid"))
    photo = logo(doc)
    photos = gallery(doc)
    if photo and photo.startswith("https://") and photo not in photos:
        photos = [photo, *photos][:MAX_PHOTOS]
    category = data.get("category") if data.get("category") in CATEGORIES else None
    categories = [c for c in (doc.get("categories") or []) if c in CATEGORIES]
    if category and category not in categories:
        categories = [category, *categories]
    return BusinessOut(
        id=data["id"],
        name=text(data.get("name")),
        phone=phones(doc.get("phone", doc.get("phones"))),
        email=optional_text(data.get("email")),
        description=optional_text(data.get("description")),
        category=category,
        categories=categories,
        city=text(data.get("city")),
        status=business_status(doc),
        visible=bool(doc.get("visible", True)),
        address=text(data.get("address")),
        timezone=valid_timezone(data.get("timezone")) or DEFAULT_TIMEZONE,
        opening_hours=OpeningHours(**hours_doc(doc.get("opening_hours"))),
        website=optional_text(data.get("website")),
        location=legacy_point(doc),
        photo=photo,
        photos=photos,
        owner_uid=owner_uid,
        owner_email=optional_text(data.get("owner_email")),
        owned=bool(owner_uid),
        instagram=optional_text(data.get("instagram")),
        locale=business_locale(doc),
        rejection_reason=optional_text(data.get("rejection_reason")),
        field_errors=notes(doc.get("field_errors")),
        admin_notes=notes(doc.get("admin_notes")),
        reviewed_at=data.get("reviewed_at"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        submitted_at=data.get("submitted_at"),
        onboarding_step=onboarding_step_of(doc),
    )


def main_branch(rows: list[dict]) -> dict | None:
    """The branch the business card mirrors: flagged main, else the first live, else the oldest."""
    if not rows:
        return None
    flagged = [row for row in rows if row.get("is_main")]
    live = [row for row in flagged if is_public_branch(row)] or [row for row in rows if is_public_branch(row)]
    if live:
        return live[0]
    return flagged[0] if flagged else rows[0]


def mirror_fields(business: dict, rows: list[dict]) -> dict:
    """Business fields that older clients and admin filters read from the business itself."""
    main = main_branch(rows)
    categories: list[str] = []
    for row in rows:
        for category in branch_categories(row, business, public_only=False):
            if category not in categories:
                categories.append(category)
    if main is None:
        return {
            "categories": categories,
            "category": categories[0] if categories else (business.get("category") if business.get("category") in CATEGORIES else None),
        }
    main_categories = branch_categories(main, business, public_only=False)
    for category in main_categories:
        if category in categories:
            categories.remove(category)
    categories = [*main_categories, *categories]
    return {
        "category": categories[0] if categories else None,
        "categories": categories,
        "city": text(main.get("city")),
        "address": text(main.get("address")),
        "timezone": branch_timezone(main, business),
        "opening_hours": branch_hours(main, business),
        "location": point_doc(main.get("location")),
    }


def compute_business_status(current: str, rows: list[dict]) -> str:
    """The business follows its branches. Suspended stays suspended."""
    if current == "suspended":
        return current
    statuses = {branch_status(row) for row in rows}
    pending_services = any(
        service["status"] == "pending_review" for row in rows for service in services_of(row)
    )
    if "published" in statuses:
        return "published"
    if current == "published" and not rows:
        return "published"
    if "pending_review" in statuses or pending_services:
        return "pending_review"
    if "rejected" in statuses:
        return "rejected"
    return "draft"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


async def list_branches(db: AsyncIOMotorDatabase, business_id: str) -> list[dict]:
    return await db.business_locations.find({"business_id": business_id}).sort("_id", 1).to_list(None)


async def branches_for(db: AsyncIOMotorDatabase, business_ids: list[str]) -> dict[str, list[dict]]:
    if not business_ids:
        return {}
    rows = await db.business_locations.find({"business_id": {"$in": business_ids}}).to_list(None)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("business_id")), []).append(row)
    for rows_for_business in grouped.values():
        rows_for_business.sort(key=lambda row: str(row.get("_id")))
    return grouped


def legacy_branch(business: dict, now: datetime) -> dict | None:
    """One branch from a business that still carries its address itself."""
    point = legacy_point(business)
    address = text(business.get("address"))
    if not address and not point:
        return None
    status = business_status(business)
    branch_state = status if status in BRANCH_STATUSES else "published"
    hours = hours_doc(business.get("opening_hours"))
    category = text(business.get("category"))
    services = (
        [build_service(category, hours, [], status=branch_state, now=now, created_at=business.get("created_at") or now)]
        if category in CATEGORIES
        else []
    )
    return {
        "business_id": str(business["_id"]),
        "is_main": True,
        "address": address,
        "city": text(business.get("city")),
        "timezone": valid_timezone(business.get("timezone")) or DEFAULT_TIMEZONE,
        "location": point,
        "place_ref": None,
        "services": services,
        "opening_hours": hours,
        "phone": [],
        "website": None,
        "instagram": None,
        "photo": None,
        "status": branch_state,
        "visible": True,
        "hidden_reason": None,
        "hidden_by": None,
        "source": "migrated",
        "field_errors": notes(business.get("field_errors")),
        "admin_notes": notes(business.get("admin_notes")),
        "rejection_reason": optional_text(business.get("rejection_reason")),
        "submitted_at": business.get("submitted_at"),
        "reviewed_at": business.get("reviewed_at"),
        "reviewed_by": optional_text(business.get("reviewed_by")),
        "created_at": business.get("created_at") or now,
        "updated_at": now,
    }


async def ensure_branches(db: AsyncIOMotorDatabase, business: dict) -> list[dict]:
    """A business that still has its address on itself gets one branch row."""
    rows = await list_branches(db, str(business["_id"]))
    if rows:
        return rows
    doc = legacy_branch(business, datetime.now(timezone.utc))
    if doc is None:
        return []
    result = await db.business_locations.insert_one(doc)
    doc["_id"] = result.inserted_id
    return [doc]


def branch_upgrade(branch: dict, business: dict, now: datetime) -> dict | None:
    """Fields a branch written by an older version is missing. None when complete."""
    update: dict = {}
    if not isinstance(branch.get("services"), list):
        update["services"] = services_of(branch, business)
    if "is_main" not in branch:
        update["is_main"] = False
    if "visible" not in branch:
        update["visible"] = True
    if branch.get("status") not in BRANCH_STATUSES:
        update["status"] = "published"
    for key, value in (
        ("place_ref", None),
        ("website", None),
        ("instagram", None),
        ("photo", None),
        ("hidden_reason", None),
        ("hidden_by", None),
        ("field_errors", {}),
        ("admin_notes", {}),
        ("rejection_reason", None),
        ("submitted_at", None),
        ("reviewed_at", None),
        ("reviewed_by", None),
    ):
        if key not in branch:
            update[key] = value
    if "source" not in branch:
        update["source"] = "migrated"
    if "created_at" not in branch:
        update["created_at"] = now
    if not update:
        return None
    update["updated_at"] = branch.get("updated_at") or now
    return update


async def sync_business(db: AsyncIOMotorDatabase, business: dict) -> dict:
    """Recompute the business status and mirrored fields from its branches.

    Returns the updated business document.
    """
    rows = await ensure_branches(db, business)
    if rows and not any(row.get("is_main") for row in rows):
        main = main_branch(rows)
        if main is not None:
            await db.business_locations.update_one({"_id": main["_id"]}, {"$set": {"is_main": True}})
            main["is_main"] = True
    now = datetime.now(timezone.utc)
    update = mirror_fields(business, rows)
    update["status"] = compute_business_status(business_status(business), rows)
    if update["status"] == "pending_review" and business_status(business) != "pending_review":
        update["submitted_at"] = now
    if update["status"] == "published" and business_status(business) != "published":
        update["rejection_reason"] = None
        update["field_errors"] = {}
        update["reviewed_at"] = now
    update["updated_at"] = now
    unset = {key: "" for key in ("phones", "latitude", "longitude", "photo_url") if key in business}
    await db.businesses.update_one(
        {"_id": business["_id"]},
        {"$set": update, **({"$unset": unset} if unset else {})},
    )
    for key in unset:
        business.pop(key, None)
    business.update(update)
    return business


async def record_event(
    db: AsyncIOMotorDatabase,
    *,
    business: dict,
    kind: str,
    audience: str,
    actor: dict | None = None,
    branch: dict | None = None,
    message: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    await db.business_events.insert_one(
        {
            "business_id": str(business["_id"]),
            "business_name": text(business.get("name")),
            "location_id": str(branch["_id"]) if branch and branch.get("_id") is not None else None,
            "address": text(branch.get("address")) if branch else None,
            "kind": kind,
            "actor_uid": optional_text((actor or {}).get("uid")),
            "actor_email": optional_text((actor or {}).get("email")),
            "audience": audience if audience in {"owner", "admins"} else "owner",
            "message": optional_text(message),
            "created_at": now,
        }
    )


def event_out(doc: dict, unread: bool = False) -> EventOut:
    data = doc_to_dict(doc)
    return EventOut(
        id=data["id"],
        business_id=str(doc.get("business_id") or ""),
        business_name=text(doc.get("business_name")),
        location_id=optional_text(doc.get("location_id")),
        address=optional_text(doc.get("address")),
        kind=text(doc.get("kind")) or "updated",
        actor_email=optional_text(doc.get("actor_email")),
        audience=doc.get("audience") if doc.get("audience") in {"owner", "admins"} else "owner",
        message=optional_text(doc.get("message")),
        created_at=utc(doc.get("created_at")) or datetime.now(timezone.utc),
        unread=unread,
    )
