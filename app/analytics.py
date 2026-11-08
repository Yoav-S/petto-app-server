"""First-party listing analytics for owners, branch owners, and Ragly admins.

Firebase / GA4 stays on the Ragly console. These counts are anonymous (hashed
visitor, no names or contact details). Events are stored per branch and rolled
up to the business and to the whole app for admins.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Literal
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
    AnalyticsSlice,
    AnalyticsTotals,
    ListingAnalytics,
)

AnalyticsScope = Literal["branch", "business", "platform"]

_ACTIONS = {
    "impression": "impressions",
    "view": "views",
    "call": "calls",
    "directions": "directions",
    "website": "website",
    "instagram": "instagram",
}
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


async def allowed_branches(
    db: AsyncIOMotorDatabase,
    business: dict,
    user: dict,
) -> list[dict]:
    """Branches this user may read. Empty + managed_location_ids() == [] means 403."""
    allowed = await managed_location_ids(db, business, user)
    rows = await docs.list_branches(db, str(business["_id"]))
    if allowed is None:
        return rows
    allow = set(allowed)
    return [row for row in rows if str(row["_id"]) in allow]


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


async def require_business_analytics(
    db: AsyncIOMotorDatabase,
    business: dict,
    user: dict,
) -> list[dict]:
    """Every branch this user may read. Branch owners with no assignment get 403."""
    allowed = await managed_location_ids(db, business, user)
    if allowed is not None and not allowed:
        raise_api_error(403, ErrorCode.UNAUTHORIZED)
    return await allowed_branches(db, business, user)


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

    field = _ACTIONS[action]
    inc = {field: 1}
    business_id = str(business["_id"])
    if action == "view":
        seen = await db.analytics_visitors.find_one(
            {"location_id": location_id, "day": day, "visitor": visitor}
        )
        if seen is None:
            try:
                await db.analytics_visitors.insert_one(
                    {
                        "business_id": business_id,
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
                "business_id": business_id,
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


def _counts(row: dict) -> dict[str, int]:
    return {
        "impressions": int(row.get("impressions") or 0),
        "views": int(row.get("views") or 0),
        "unique_visitors": int(row.get("unique_visitors") or 0),
        "calls": int(row.get("calls") or 0),
        "directions": int(row.get("directions") or 0),
        "website": int(row.get("website") or 0),
        "instagram": int(row.get("instagram") or 0),
    }


def _add(
    target: AnalyticsTotals | AnalyticsDay | AnalyticsHour,
    counts: dict[str, int] | None = None,
    *,
    impressions: int = 0,
    views: int = 0,
    unique_visitors: int = 0,
    calls: int = 0,
    directions: int = 0,
    website: int = 0,
    instagram: int = 0,
) -> None:
    payload = counts or {
        "impressions": impressions,
        "views": views,
        "unique_visitors": unique_visitors,
        "calls": calls,
        "directions": directions,
        "website": website,
        "instagram": instagram,
    }
    target.impressions += payload.get("impressions", 0)
    target.views += payload.get("views", 0)
    target.unique_visitors += payload.get("unique_visitors", 0)
    target.calls += payload.get("calls", 0)
    target.directions += payload.get("directions", 0)
    target.website += payload.get("website", 0)
    target.instagram += payload.get("instagram", 0)


def _busy_hours(by_hour: list[AnalyticsHour], total_views: int) -> list[AnalyticsBusyHour]:
    ranked = sorted(by_hour, key=lambda item: (-item.views, item.hour))
    busy: list[AnalyticsBusyHour] = []
    for slot in ranked[:3]:
        if slot.views <= 0:
            break
        share = (slot.views / total_views) if total_views else 0.0
        busy.append(AnalyticsBusyHour(hour=slot.hour, views=slot.views, share=round(share, 3)))
    return busy


def _slice_name(address: str, city: str, fallback: str) -> str:
    line = ", ".join(part for part in (address, city) if part)
    return line or fallback


def _sort_slices(rows: list[AnalyticsSlice]) -> list[AnalyticsSlice]:
    return sorted(
        rows,
        key=lambda row: (
            -row.totals.calls,
            -row.totals.views,
            -row.totals.impressions,
            row.name.lower(),
        ),
    )


async def _summarize(
    db: AsyncIOMotorDatabase,
    *,
    scope: AnalyticsScope,
    days: int,
    tz: ZoneInfo,
    location_ids: list[str],
    business_id: str | None = None,
    location_id: str | None = None,
    name: str = "",
    address: str = "",
    city: str = "",
    branch_meta: dict[str, tuple[str, str]] | None = None,
    business_meta: dict[str, str] | None = None,
    location_business: dict[str, str] | None = None,
) -> ListingAnalytics:
    days = max(1, min(int(days), _RANGE_MAX))
    end = datetime.now(tz).date()
    start = end - timedelta(days=days - 1)
    start_s = start.isoformat()
    end_s = end.isoformat()

    day_filter = {"day": {"$gte": start_s, "$lte": end_s}}
    hour_query: dict = dict(day_filter)
    visitor_query: dict = dict(day_filter)
    if location_ids:
        hour_query["location_id"] = {"$in": location_ids}
        visitor_query["location_id"] = {"$in": location_ids}
    elif scope != "platform":
        hour_query["location_id"] = {"$in": []}
        visitor_query["location_id"] = {"$in": []}

    rows = await db.analytics_hours.find(hour_query).to_list(None) if (
        location_ids or scope == "platform"
    ) else []
    visitor_rows = await db.analytics_visitors.find(visitor_query).to_list(None) if (
        location_ids or scope == "platform"
    ) else []

    by_day: dict[str, AnalyticsDay] = {}
    cursor = start
    while cursor <= end:
        key = cursor.isoformat()
        by_day[key] = AnalyticsDay(date=key)
        cursor += timedelta(days=1)
    by_hour = _empty_hours()
    totals = AnalyticsTotals()
    per_location = {lid: AnalyticsTotals() for lid in location_ids}
    per_business: dict[str, AnalyticsTotals] = {}

    for row in rows:
        date = str(row.get("day") or "")
        hour = int(row.get("hour") or 0)
        counts = _counts(row)
        counted = {**counts, "unique_visitors": 0}
        if date in by_day:
            _add(by_day[date], counts)
        if 0 <= hour <= 23:
            _add(by_hour[hour], counts)
        _add(totals, counted)
        loc = str(row.get("location_id") or "")
        if loc in per_location:
            _add(per_location[loc], counted)
        bid = str(row.get("business_id") or "") or (location_business or {}).get(loc, "")
        if bid:
            per_business.setdefault(bid, AnalyticsTotals())
            _add(per_business[bid], counted)

    people: set[str] = set()
    per_day: dict[str, set[str]] = {}
    loc_people: dict[str, set[str]] = {}
    business_people: dict[str, set[str]] = {}
    for row in visitor_rows:
        visitor = str(row.get("visitor") or "")
        if not visitor:
            continue
        loc = str(row.get("location_id") or "")
        bid = str(row.get("business_id") or "") or (location_business or {}).get(loc, "")
        people.add(visitor)
        per_day.setdefault(str(row.get("day") or ""), set()).add(visitor)
        loc_people.setdefault(loc, set()).add(visitor)
        if bid:
            business_people.setdefault(bid, set()).add(visitor)
    totals.unique_visitors = len(people)
    for date, visitors in per_day.items():
        if date in by_day:
            by_day[date].unique_visitors = len(visitors)
    for loc, visitors in loc_people.items():
        if loc in per_location:
            per_location[loc].unique_visitors = len(visitors)
    for bid, visitors in business_people.items():
        per_business.setdefault(bid, AnalyticsTotals()).unique_visitors = len(visitors)

    branches: list[AnalyticsSlice] = []
    if scope == "business" and branch_meta:
        for loc in location_ids:
            addr, city_name = branch_meta.get(loc, ("", ""))
            branches.append(
                AnalyticsSlice(
                    id=loc,
                    name=_slice_name(addr, city_name, loc),
                    address=addr,
                    city=city_name,
                    totals=per_location.get(loc, AnalyticsTotals()),
                )
            )
        branches = _sort_slices(branches)

    businesses: list[AnalyticsSlice] = []
    if scope == "platform" and business_meta:
        for bid, biz_name in business_meta.items():
            businesses.append(
                AnalyticsSlice(
                    id=bid,
                    name=biz_name or bid,
                    totals=per_business.get(bid, AnalyticsTotals()),
                )
            )
        businesses = _sort_slices(businesses)

    return ListingAnalytics(
        scope=scope,
        business_id=business_id,
        location_id=location_id,
        name=name,
        address=address,
        city=city,
        timezone=str(tz),
        days=days,
        totals=totals,
        by_day=list(by_day.values()),
        by_hour=by_hour,
        busy_hours=_busy_hours(by_hour, totals.views),
        branches=branches,
        businesses=businesses,
    )


async def read_branch(
    db: AsyncIOMotorDatabase,
    business: dict,
    branch: dict,
    days: int,
) -> ListingAnalytics:
    tz = _zone(docs.text(branch.get("timezone")) or business.get("timezone"))
    return await _summarize(
        db,
        scope="branch",
        days=days,
        tz=tz,
        location_ids=[str(branch["_id"])],
        business_id=str(business["_id"]),
        location_id=str(branch["_id"]),
        name=docs.text(business.get("name")),
        address=docs.text(branch.get("address")),
        city=docs.text(branch.get("city")),
    )


async def read_business(
    db: AsyncIOMotorDatabase,
    business: dict,
    branches: list[dict],
    days: int,
) -> ListingAnalytics:
    tz = _zone(business.get("timezone"))
    meta = {
        str(row["_id"]): (docs.text(row.get("address")), docs.text(row.get("city")))
        for row in branches
    }
    return await _summarize(
        db,
        scope="business",
        days=days,
        tz=tz,
        location_ids=[str(row["_id"]) for row in branches],
        business_id=str(business["_id"]),
        name=docs.text(business.get("name")),
        branch_meta=meta,
    )


async def read_platform(
    db: AsyncIOMotorDatabase,
    days: int,
) -> ListingAnalytics:
    tz = _zone(settings.DEFAULT_TIMEZONE)
    businesses = await db.businesses.find({}).sort("name", 1).to_list(None)
    grouped = await docs.branches_for(db, [str(row["_id"]) for row in businesses])
    location_business: dict[str, str] = {}
    location_ids: list[str] = []
    business_meta: dict[str, str] = {}
    for row in businesses:
        bid = str(row["_id"])
        business_meta[bid] = docs.text(row.get("name")) or bid
        for branch in grouped.get(bid, []):
            lid = str(branch["_id"])
            location_ids.append(lid)
            location_business[lid] = bid
    return await _summarize(
        db,
        scope="platform",
        days=days,
        tz=tz,
        location_ids=location_ids,
        name="Ragly",
        business_meta=business_meta,
        location_business=location_business,
    )
