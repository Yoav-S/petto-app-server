"""
reminder_series.py — Materialize one Mongo document per repeating occurrence.

A repeating reminder is a series: when today's item fires (or is marked
Done/Missed), it stays in Recent with that day's mark and a new document is
created for the next date. Skipped days while the dispatcher was down are
inserted as their own rows so each day can be marked independently.
"""
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

from app.core.scheduling import (
    compute_scheduled_at,
    next_occurrence,
    occurrence_within_end,
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

_MAX_FOLLOW_ON = 800


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


async def spawn_following_occurrences(
    db,
    reminder: dict,
    tz_name: str | None,
    now: datetime | None = None,
) -> list[dict]:
    """
    Create later dates in this series until there is a still-future head.

    The source row is left unchanged (it becomes the Recent item). Overdue
    follow-on dates are inserted with notified_at set so they land in Recent
    awaiting Done/Missed without extra pushes. The first future date is a
    clean Upcoming (or Today) item.
    """
    repeat = reminder.get("repeat") or "off"
    if not next_occurrence(reminder.get("date", ""), repeat):
        return []

    now_utc = now or datetime.now(timezone.utc)
    series_id = await ensure_series_id(db, reminder)
    time_str = reminder.get("time", "")
    end_date = reminder.get("end_date")
    created: list[dict] = []
    candidate = next_occurrence(reminder.get("date", ""), repeat)

    for _ in range(_MAX_FOLLOW_ON):
        if not candidate or not occurrence_within_end(candidate, end_date):
            break

        existing = await db.reminders.find_one(
            {"series_id": series_id, "date": candidate}
        )
        scheduled = compute_scheduled_at(candidate, time_str, tz_name)
        if existing:
            if scheduled and scheduled <= now_utc:
                candidate = next_occurrence(candidate, repeat)
                continue
            break

        if scheduled is None:
            break

        doc = _occurrence_template(reminder, candidate, series_id, now_utc)
        overdue = scheduled <= now_utc
        if overdue:
            # Already past — Recent + awaiting ack, no extra push this tick.
            # This walk also creates later dates, so don't retry spawn from here.
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

        if overdue:
            candidate = next_occurrence(candidate, repeat)
            continue
        break

    await db.reminders.update_one(
        {"_id": reminder["_id"]},
        {"$set": {"series_id": series_id, "next_spawned": True}},
    )
    reminder["series_id"] = series_id
    reminder["next_spawned"] = True
    return created
