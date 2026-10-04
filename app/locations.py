"""Branch rows for a business. The brand stays on the business document."""

from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.utils import doc_to_dict
from app.models.business import LocationOut, LocationWrite, OpeningHours, PlaceLocation


def _hours(raw: object) -> dict:
    source = raw if isinstance(raw, dict) else {}
    hours: dict = {"always_open": bool(source.get("always_open", False))}
    for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        slots = []
        for slot in source.get(day) or []:
            if isinstance(slot, dict) and slot.get("open") and slot.get("close"):
                slots.append({"open": str(slot["open"]), "close": str(slot["close"])})
        hours[day] = slots
    return hours


def _point(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    coords = raw.get("coordinates")
    if not isinstance(coords, list) or len(coords) != 2:
        return None
    try:
        return {"type": "Point", "coordinates": [float(coords[0]), float(coords[1])]}
    except (TypeError, ValueError):
        return None


def _phones(raw: object) -> list[str]:
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def location_out(doc: dict) -> LocationOut:
    data = doc_to_dict(doc)
    point = _point(doc.get("location"))
    return LocationOut(
        id=data["id"],
        business_id=str(doc.get("business_id") or ""),
        address=_text(doc.get("address")),
        city=_text(doc.get("city")),
        timezone=_text(doc.get("timezone")) or "Europe/Chisinau",
        opening_hours=OpeningHours(**_hours(doc.get("opening_hours"))),
        location=point or {"type": "Point", "coordinates": [0.0, 0.0]},
        phone=_phones(doc.get("phone")),
    )


def place_location(
    doc: dict,
    business_phone: list[str],
    distance_km: float | None,
) -> PlaceLocation:
    branch_phone = _phones(doc.get("phone"))
    shared = not branch_phone and bool(business_phone)
    point = _point(doc.get("location"))
    return PlaceLocation(
        id=str(doc.get("_id")),
        address=_text(doc.get("address")),
        city=_text(doc.get("city")),
        opening_hours=OpeningHours(**_hours(doc.get("opening_hours"))),
        phone=branch_phone,
        call_phone=branch_phone or business_phone,
        shared_phone=shared,
        location=point,
        distance_km=distance_km,
    )


async def list_location_docs(db: AsyncIOMotorDatabase, business_id: str) -> list[dict]:
    return await db.business_locations.find({"business_id": business_id}).sort("_id", 1).to_list(None)


async def locations_for(db: AsyncIOMotorDatabase, business_ids: list[str]) -> dict[str, list[dict]]:
    if not business_ids:
        return {}
    rows = await db.business_locations.find({"business_id": {"$in": business_ids}}).to_list(None)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("business_id")), []).append(row)
    for rows_for_business in grouped.values():
        rows_for_business.sort(key=lambda row: str(row.get("_id")))
    return grouped


async def ensure_locations(db: AsyncIOMotorDatabase, business: dict) -> list[dict]:
    """Existing single-address listings become one branch. The general phone stays on the business."""
    business_id = str(business["_id"])
    rows = await list_location_docs(db, business_id)
    if rows:
        return rows
    point = _point(business.get("location"))
    address = _text(business.get("address"))
    if not address and not point:
        return []
    now = datetime.now(timezone.utc)
    doc = {
        "business_id": business_id,
        "address": address,
        "city": _text(business.get("city")),
        "timezone": _text(business.get("timezone")) or "Europe/Chisinau",
        "opening_hours": _hours(business.get("opening_hours")),
        "location": point,
        "phone": [],
        "created_at": now,
        "updated_at": now,
    }
    result = await db.business_locations.insert_one(doc)
    doc["_id"] = result.inserted_id
    return [doc]


def location_payload(body: LocationWrite) -> dict:
    longitude, latitude = body.location.coordinates
    return {
        "address": body.address.strip(),
        "city": body.city.strip(),
        "timezone": body.timezone.strip(),
        "opening_hours": body.opening_hours.model_dump(),
        "location": {"type": "Point", "coordinates": [longitude, latitude]},
        "phone": body.phone,
    }


def mirror_fields(location: dict) -> dict:
    """Keep the business card fields aligned with the first branch for older clients."""
    return {
        "address": _text(location.get("address")),
        "city": _text(location.get("city")) or None,
        "timezone": _text(location.get("timezone")) or "Europe/Chisinau",
        "opening_hours": _hours(location.get("opening_hours")),
        "location": _point(location.get("location")),
    }
