"""First-party listing analytics for owners, branch owners, and Ragly admins.

Firebase / GA4 stays on the Ragly console. These counts are anonymous (hashed
visitor, no names or contact details) and scoped to one branch at a time.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bson import ObjectId
from fastapi import Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app import business_docs as docs
from app.core.config import settings
from app.core.errors import ErrorCode, raise_api_error
from app.models.business import (
    AnalyticsBusyHour,
    AnalyticsDay,
    AnalyticsHour,
    AnalyticsTotals,
    BranchAnalytics,
)

_ACTIONS = {"view", "call", "directions"}
_DEDUPE_SECONDS = 20 * 60
_RANGE_MAX = 90


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or settings.DEFAULT_TIMEZONE or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _visitor_hash(uid: str | None, ip: str, ua: str) -> str:
    material = f"uid:{uid}" if uid else f"anon:{ip}:{ua[:120]}"
    key = (settings.INTERNAL_TASK_SECRET or settings.FIREBASE_PROJECT_ID or "ragly").encode()
    return hmac.new(key, material.encode(), hashlib.sha256).hexdigest()[:32]


def _client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return ""


def _is_admin(user: dict | None) -> bool:
    return bool(user and settings.is_ragly_admin(user.get("email")))


async def _listing_owner(db: AsyncIOMotorDatabase, business: dict, uid: str) -> bool:
    if business.get("owner_uid") == uid:
        return True
    member = await db.business_members.find_one(
        {"business_id": str(business["_id"]), "user_id": uid, "role": "owner"}
    )
    return bool(member)


async def managed_location_ids(
    db: AsyncIOMotorDatabase,
    business: dict,
    user: dict,
) -> list[str] | None:
    """None means every branch. An empty list means no access."""
    if _is_admin(user):
        return None
    uid = user.get("uid") or ""
    if await _listing_owner(db, business, uid):
        return None
    rows = await db.business_members.find(
        {
            "business_id": str(business["_id"]),
            "user_id": uid,
            "role": "branch_owner",
        }
    ).to_list(None)
    return [lid for row in rows if (lid := docs.optional_text(row.get("location_id")))]


async def require_branch_analytics(
    db: AsyncIOMotorDatabase,
    business: dict,
    user: dict,
    location_id: str,
) -> dict:
    if not ObjectId.is_valid(location_id):
        raise_api_error(404, ErrorCode.NOT_FOUND)
    branch = await db.business_locations.find_one(
        {"_id": ObjectId(location_id), "business_id": str(business["_id"])}
    )
    if not branch:
        raise_api_error(404, ErrorCode.NOT_FOUND)
    allowed = await managed_location_ids(db, business, user)
    if allowed is not None and location_id not in allowed:
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    return branch


async def _is_staff(db: AsyncIOMotorDatabase, business: dict, uid: str) -> bool:
    if business.get("owner_uid") == uid:
        return True
    member = await db.business_members.find_one(
        {"business_id": str(business["_id"]), "user_id": uid}
    )
    return bool(member)


async def record_event(
    db: AsyncIOMotorDatabase,
    request: Request,
    business: dict,
    location_id: str,
    action: str,
    user: dict | None,
) -> None:
    if action not in _ACTIONS:
        return
    if not ObjectId.is_valid(location_id):
        return
    if docs.business_status(business) != "published" or not docs.business_visible(business):
        return
    branch = await db.business_locations.find_one(
        {"_id": ObjectId(location_id), "business_id": str(business["_id"])}
    )
    if not branch or branch.get("status") != "published" or branch.get("visible", True) is False:
        return
    uid = (user or {}).get("uid")
    if uid and await _is_staff(db, business, uid):
        return

    now = datetime.now(_zone(docs.text(branch.get("timezone")) or business.get("timezone")))
    day = now.date().isoformat()
    hour = now.hour
    visitor = _visitor_hash(uid, _client_ip(request), request.headers.get("user-agent") or "")
    window = int(now.timestamp() // _DEDUPE_SECONDS)
    dedupe_key = f"{location_id}:{action}:{visitor}:{window}"
    if await db.analytics_dedupe.find_one({"key": dedupe_key}):
        return
    try:
        await db.analytics_dedupe.insert_one(
            {
                "key": dedupe_key,
                "expires_at": datetime.now(timezone.utc) + timedelta(seconds=_DEDUPE_SECONDS * 2),
            }
        )
    except DuplicateKeyError:
        return

    field = {"view": "views", "call": "calls", "directions": "directions"}[action]
    inc = {field: 1}
    if action == "view":
        seen = await db.analytics_visitors.find_one(
            {"location_id": location_id, "day": day, "visitor": visitor}
        )
        if seen is None:
            try:
                await db.analytics_visitors.insert_one(
                    {
                        "location_id": location_id,
                        "day": day,
                        "visitor": visitor,
                    }
                )
                inc["unique_visitors"] = 1
            except DuplicateKeyError:
                pass

    await db.analytics_hours.update_one(
        {"location_id": location_id, "day": day, "hour": hour},
        {
            "$setOnInsert": {
                "business_id": str(business["_id"]),
                "location_id": location_id,
                "day": day,
                "hour": hour,
            },
            "$inc": inc,
        },
        upsert=True,
    )


def _empty_hours() -> list[AnalyticsHour]:
    return [AnalyticsHour(hour=hour) for hour in range(24)]


async def read_branch(
    db: AsyncIOMotorDatabase,
    business: dict,
    branch: dict,
    days: int,
) -> BranchAnalytics:
    days = max(1, min(int(days), _RANGE_MAX))
    tz = _zone(docs.text(branch.get("timezone")) or business.get("timezone"))
    end = datetime.now(tz).date()
    start = end - timedelta(days=days - 1)
    start_s = start.isoformat()
    end_s = end.isoformat()
    location_id = str(branch["_id"])
    rows = await db.analytics_hours.find(
        {"location_id": location_id, "day": {"$gte": start_s, "$lte": end_s}}
    ).to_list(None)

    by_day: dict[str, AnalyticsDay] = {}
    cursor = start
    while cursor <= end:
        key = cursor.isoformat()
        by_day[key] = AnalyticsDay(date=key)
        cursor += timedelta(days=1)
    by_hour = _empty_hours()
    totals = AnalyticsTotals()

    for row in rows:
        date = str(row.get("day") or "")
        hour = int(row.get("hour") or 0)
        views = int(row.get("views") or 0)
        uniques = int(row.get("unique_visitors") or 0)
        calls = int(row.get("calls") or 0)
        directions = int(row.get("directions") or 0)
        if date in by_day:
            day_row = by_day[date]
            day_row.views += views
            day_row.unique_visitors += uniques
            day_row.calls += calls
            day_row.directions += directions
        if 0 <= hour <= 23:
            slot = by_hour[hour]
            slot.views += views
            slot.unique_visitors += uniques
            slot.calls += calls
            slot.directions += directions
        totals.views += views
        totals.calls += calls
        totals.directions += directions

    visitor_rows = await db.analytics_visitors.find(
        {"location_id": location_id, "day": {"$gte": start_s, "$lte": end_s}}
    ).to_list(None)
    people: set[str] = set()
    per_day: dict[str, set[str]] = {}
    for row in visitor_rows:
        visitor = str(row.get("visitor") or "")
        if not visitor:
            continue
        people.add(visitor)
        per_day.setdefault(str(row.get("day") or ""), set()).add(visitor)
    totals.unique_visitors = len(people)
    for date, visitors in per_day.items():
        if date in by_day:
            by_day[date].unique_visitors = len(visitors)

    ranked = sorted(by_hour, key=lambda item: (-item.views, item.hour))
    busy: list[AnalyticsBusyHour] = []
    for slot in ranked[:3]:
        if slot.views <= 0:
            break
        share = (slot.views / totals.views) if totals.views else 0.0
        busy.append(AnalyticsBusyHour(hour=slot.hour, views=slot.views, share=round(share, 3)))

    return BranchAnalytics(
        business_id=str(business["_id"]),
        location_id=location_id,
        address=docs.text(branch.get("address")),
        city=docs.text(branch.get("city")),
        timezone=str(tz),
        days=days,
        totals=totals,
        by_day=list(by_day.values()),
        by_hour=by_hour,
        busy_hours=busy,
    )
