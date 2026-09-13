"""
reminder_series.py — One live occurrence per repeating series.

A series has at most one scheduled head (Today or Upcoming). When that
head fires or is marked Done/Missed it stays in Recent, and exactly one
next date is inserted if it is still within end_date.

Missed days while the dispatcher was down are filled into Recent only for
the last MAX_CATCH_UP_DAYS. Older gaps are skipped so a missing end date
cannot create an unbounded Upcoming list.
"""
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from app.core.scheduling import (
    compute_scheduled_at,
    next_occurrence,
    next_occurrence_on_or_after,
    occurrence_within_end,
    resolve_timezone,
)

_COPY_FIELDS = (
    "title",
    "time",
    "repeat",
    "end_date",
    "alert",
    "note",
    "category",
    "pet_id",
)

MAX_CATCH_UP_DAYS = 14
_MAX_WALK = 400


def _occurrence_template(source: dict, date_str: str, series_id: str, now: datetime) -> dict:
    doc = {field: source.get(field) for field in _COPY_FIELDS}
    doc["date"] = date_str
    doc["end_date"] = source.get("end_date") or None
    doc["alert"] = source.get("alert") or "off"
    doc["repeat"] = source.get("repeat") or "off"
    doc["category"] = source.get("category") or "general"
    doc["status"] = "scheduled"
    doc["notified_at"] = None
    doc["alert_notified_at"] = None
    doc["next_spawned"] = False
    doc["series_id"] = series_id
    doc["created_at"] = now
    return doc


async def ensure_series_id(db, reminder: dict) -> str:
    """Persist series_id on older rows that predate occurrence documents."""
    existing = reminder.get("series_id")
    if existing:
        return existing
    sid = str(reminder["_id"])
    await db.reminders.update_one({"_id": reminder["_id"]}, {"$set": {"series_id": sid}})
    reminder["series_id"] = sid
    return sid


async def prune_extra_future_heads(
    db,
    series_id: str,
    today_str: str,
    *,
    keep_id=None,
) -> None:
    """Upcoming may hold only the soonest scheduled date for a series."""
    futures = await db.reminders.find(
        {
            "series_id": series_id,
            "status": "scheduled",
            "date": {"$gt": today_str},
        },
        sort=[("date", 1), ("time", 1), ("_id", 1)],
    ).to_list(None)
    if not futures:
        return
    keep = keep_id if keep_id is not None else futures[0]["_id"]
    extra_ids = [doc["_id"] for doc in futures if doc["_id"] != keep]
    if extra_ids:
        await db.reminders.delete_many({"_id": {"$in": extra_ids}})


async def _mark_spawned(db, reminder: dict, series_id: str) -> None:
    await db.reminders.update_one(
        {"_id": reminder["_id"]},
        {"$set": {"series_id": series_id, "next_spawned": True}},
    )
    reminder["series_id"] = series_id
    reminder["next_spawned"] = True


async def spawn_following_occurrences(
    db,
    reminder: dict,
    tz_name: str | None,
    now: datetime | None = None,
) -> list[dict]:
    """
    Keep this row in Recent and materialize at most one live next date.

    Overdue dates inside the last MAX_CATCH_UP_DAYS become Recent rows
    (awaiting Done/Missed, no extra push). The first still-future date
    becomes the single Upcoming / Today head.
    """
    repeat = reminder.get("repeat") or "off"
    if not next_occurrence(reminder.get("date", ""), repeat):
        return []

    now_utc = now or datetime.now(timezone.utc)
    tz = resolve_timezone(tz_name)
    local_today = now_utc.astimezone(tz).date()
    today_str = local_today.isoformat()
    cutoff = (local_today - timedelta(days=MAX_CATCH_UP_DAYS)).isoformat()

    series_id = await ensure_series_id(db, reminder)
    time_str = reminder.get("time", "")
    end_date = reminder.get("end_date")
    created: list[dict] = []

    existing_future = await db.reminders.find_one(
        {
            "series_id": series_id,
            "status": "scheduled",
            "date": {"$gt": today_str},
        },
        sort=[("date", 1), ("time", 1)],
    )
    if existing_future:
        await prune_extra_future_heads(
            db, series_id, today_str, keep_id=existing_future["_id"]
        )
        await _mark_spawned(db, reminder, series_id)
        return []

    today_live = await db.reminders.find_one(
        {
            "series_id": series_id,
            "status": "scheduled",
            "date": today_str,
            "_id": {"$ne": reminder["_id"]},
            "$or": [
                {"notified_at": None},
                {"notified_at": {"$exists": False}},
            ],
        }
    )
    if today_live:
        await prune_extra_future_heads(db, series_id, today_str, keep_id=today_live["_id"])
        await _mark_spawned(db, reminder, series_id)
        return []

    candidate = next_occurrence_on_or_after(reminder.get("date", ""), repeat, cutoff)
    for _ in range(_MAX_WALK):
        if not candidate or not occurrence_within_end(candidate, end_date):
            break

        existing = await db.reminders.find_one(
            {"series_id": series_id, "date": candidate}
        )
        scheduled = compute_scheduled_at(candidate, time_str, tz_name)
        if scheduled is None:
            break

        overdue = scheduled <= now_utc
        if existing:
            if overdue:
                candidate = next_occurrence(candidate, repeat)
                continue
            break

        if overdue:
            if candidate < cutoff:
                candidate = next_occurrence(candidate, repeat)
                continue
            doc = _occurrence_template(reminder, candidate, series_id, now_utc)
            doc["notified_at"] = now_utc
            doc["alert_notified_at"] = now_utc
            doc["next_spawned"] = True
            try:
                result = await db.reminders.insert_one(doc)
            except DuplicateKeyError:
                candidate = next_occurrence(candidate, repeat)
                continue
            doc["_id"] = result.inserted_id
            created.append(doc)
            candidate = next_occurrence(candidate, repeat)
            continue

        doc = _occurrence_template(reminder, candidate, series_id, now_utc)
        try:
            result = await db.reminders.insert_one(doc)
        except DuplicateKeyError:
            break
        doc["_id"] = result.inserted_id
        created.append(doc)
        break

    keep_future = next(
        (row["_id"] for row in created if row.get("date", "") > today_str),
        None,
    )
    await prune_extra_future_heads(db, series_id, today_str, keep_id=keep_future)
    await _mark_spawned(db, reminder, series_id)
    return created
