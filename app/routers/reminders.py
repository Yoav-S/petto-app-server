"""
reminders.py — /pets/{pet_id}/reminders endpoints.

Tab filtering (server-rules §3.3, screen analysis):
  today    → date == today, stored_status == "scheduled"
  upcoming → date >  today, stored_status == "scheduled"
  recent   → completed | missed | (date < today AND scheduled = auto-missed)

Status returned in API responses is server-computed (see utils.compute_reminder_status).
Status stored in DB is: "scheduled" | "completed" | "missed".

Deleting a reminder does NOT delete linked HealthNotes — the note keeps
the linked_reminder_id as a historical reference (the reminder display
will simply not resolve). This is intentional: notes are the primary record.
"""
from fastapi import APIRouter, Depends, Query
from typing import Optional
from app.core.errors import ErrorCode, raise_api_error
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
from datetime import datetime, timezone

from app.core.database import get_database
from app.core.scheduling import (
    resolve_timezone,
    compute_scheduled_at,
    is_alert_possible,
)
from app.core.reminder_series import spawn_following_occurrences
from app.core.subscription import can_add_active_reminder
from app.core.utils import (
    doc_to_dict,
    is_valid_object_id,
    validate_pet_ownership,
    validate_entity_ownership,
    compute_reminder_status,
    reminder_awaiting_ack,
    build_reminder_tab_query,
)
from app.middleware.auth import get_current_user
from app.models.reminder import (
    ReminderCreate,
    ReminderUpdate,
    ReminderStatusUpdate,
    ReminderOut,
)

router = APIRouter(prefix="/pets/{pet_id}/reminders", tags=["reminders"])


async def _assert_future_datetime(
    uid: str,
    date: str,
    time: str,
    db: AsyncIOMotorDatabase,
    *,
    previous_date: str | None = None,
) -> None:
    """Allow today and future dates. Reject only calendar days before today.

    Same-day reminders are always allowed at any clock time.
    """
    user = await db.users.find_one({"firebase_uid": uid})
    tz_name = (user or {}).get("timezone")
    tz = resolve_timezone(tz_name)
    date_norm = (date or "")[:10]
    # Earliest of user-local / UTC today — never treat "today on the phone"
    # as a past date because of timezone skew.
    min_today = min(
        datetime.now(tz).date(),
        datetime.now(timezone.utc).date(),
    ).isoformat()
    date_is_new = previous_date is None or date_norm != (previous_date or "")[:10]
    if date_is_new and date_norm < min_today:
        raise_api_error(422, ErrorCode.REMINDER_DATETIME_IN_PAST)

    if not compute_scheduled_at(date_norm, time, tz_name):
        raise_api_error(422, ErrorCode.FAILED_TO_SAVE)


def _normalize_end_date(end_date: str | None) -> str | None:
    value = (end_date or "").strip()[:10]
    return value or None


def _assert_end_after_start(start: str, end_date: str | None) -> None:
    end = _normalize_end_date(end_date)
    if end and end <= (start or "")[:10]:
        raise_api_error(422, ErrorCode.END_DATE_BEFORE_START)


async def _user_timezone_name(uid: str, db: AsyncIOMotorDatabase) -> str | None:
    user = await db.users.find_one({"firebase_uid": uid})
    return (user or {}).get("timezone")


async def _assert_alert_possible(
    uid: str,
    date: str,
    time: str,
    alert: str | None,
    db: AsyncIOMotorDatabase,
) -> None:
    if not alert or alert == "off":
        return
    tz_name = await _user_timezone_name(uid, db)
    if not is_alert_possible(date, time, tz_name, alert, datetime.now(timezone.utc)):
        raise_api_error(422, ErrorCode.ALERT_NOT_POSSIBLE)


def _enrich(
    doc: dict,
    today_str: str | None = None,
    *,
    now_hm: str | None = None,
) -> ReminderOut:
    """Attach server-computed status to a reminder document (in the user's tz)."""
    d = doc_to_dict(doc)
    stored_status = d.get("status", "scheduled")
    d["status"] = compute_reminder_status(
        d.get("date", ""),
        stored_status,
        today_str,
        reminder_time_str=d.get("time"),
        now_hm=now_hm,
    )
    d["awaiting_ack"] = reminder_awaiting_ack(
        stored_status,
        d.get("notified_at"),
        d.get("date", ""),
        d.get("time"),
        today_str,
        now_hm,
    )
    if not d.get("category"):
        d["category"] = "general"
    if not d.get("alert"):
        d["alert"] = "off"
    if d.get("end_date") == "":
        d["end_date"] = None
    return ReminderOut(**d)


async def _user_local_clock(
    uid: str, db: AsyncIOMotorDatabase
) -> tuple[str, str]:
    """Return (today YYYY-MM-DD, now HH:MM) in the user's timezone.

    Tab filtering and display status use the user's calendar day so a
    reminder created for "today" on the phone lands on the Today tab.
    Create validation still uses min(user_today, utc_today) only to reject
    past calendar days — see _assert_future_datetime.
    """
    user = await db.users.find_one({"firebase_uid": uid})
    tz = resolve_timezone((user or {}).get("timezone"))
    now = datetime.now(tz)
    return now.date().isoformat(), f"{now.hour:02d}:{now.minute:02d}"


async def _user_today_str(uid: str, db: AsyncIOMotorDatabase) -> str:
    """Return effective 'today' for list/status."""
    today_str, _ = await _user_local_clock(uid, db)
    return today_str


# ---------------------------------------------------------------------------
# List reminders (tab filtering)
# ---------------------------------------------------------------------------

@router.get("", response_model=list[ReminderOut])
async def list_reminders(
    pet_id: str,
    tab: str = Query("today", pattern="^(today|upcoming|recent)$"),
    limit: Optional[int] = Query(None, ge=1, le=50),
    cursor: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Return reminders for a tab.
    Sorting:
      today/upcoming → soonest first (date ASC)
      recent         → most recent first (date DESC)
    Pagination: pass limit + cursor (last item id) for the next page.
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    today_str, now_hm = await _user_local_clock(current_user["uid"], db)
    query = build_reminder_tab_query(pet_id, tab, today_str, now_hm=now_hm)
    sort_dir = 1 if tab in ("today", "upcoming") else -1
    sort = [("date", sort_dir), ("time", sort_dir), ("_id", sort_dir)]

    if cursor and is_valid_object_id(cursor):
        last = await db.reminders.find_one({"_id": ObjectId(cursor)})
        if last:
            last_date = last.get("date")
            last_time = last.get("time") or ""
            last_id = ObjectId(cursor)
            if sort_dir == 1:
                query["$or"] = [
                    {"date": {"$gt": last_date}},
                    {"date": last_date, "time": {"$gt": last_time}},
                    {"date": last_date, "time": last_time, "_id": {"$gt": last_id}},
                ]
            else:
                query["$or"] = [
                    {"date": {"$lt": last_date}},
                    {"date": last_date, "time": {"$lt": last_time}},
                    {"date": last_date, "time": last_time, "_id": {"$lt": last_id}},
                ]

    docs = await db.reminders.find(query, sort=sort).to_list(limit or None)
    if limit:
        docs = docs[:limit]
    return [_enrich(d, today_str, now_hm=now_hm) for d in docs]


# ---------------------------------------------------------------------------
# Create reminder
# ---------------------------------------------------------------------------

@router.post("", response_model=ReminderOut, status_code=201)
async def create_reminder(
    pet_id: str,
    body: ReminderCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Create a new reminder. Initial stored_status is always 'scheduled'."""
    uid = current_user["uid"]
    await validate_pet_ownership(pet_id, uid, db)

    user = await db.users.find_one({"firebase_uid": uid})
    today_str = await _user_today_str(uid, db)
    if not await can_add_active_reminder(uid, db, today_str, user_doc=user):
        raise_api_error(403, ErrorCode.PREMIUM_REQUIRED_REMINDER)

    # Today (any time) and future dates are allowed. Only reject calendar days
    # before the user's today — never block same-day creates.
    await _assert_future_datetime(uid, body.date, body.time, db)
    end_date = _normalize_end_date(body.end_date)
    _assert_end_after_start(body.date, end_date)
    await _assert_alert_possible(uid, body.date, body.time, body.alert, db)
    oid = ObjectId()
    doc = {
        **body.model_dump(),
        "_id": oid,
        "date": body.date[:10],
        "end_date": end_date,
        "alert": body.alert or "off",
        "pet_id": pet_id,
        "status": "scheduled",       # stored status — computed on read
        "notified_at": None,         # set once a push has been sent (dispatcher)
        "alert_notified_at": None,
        "next_spawned": False,
        "series_id": str(oid),
        "created_at": datetime.now(timezone.utc),
    }
    await db.reminders.insert_one(doc)
    today_str, now_hm = await _user_local_clock(uid, db)
    return _enrich(doc, today_str, now_hm=now_hm)


# ---------------------------------------------------------------------------
# Get single reminder
# ---------------------------------------------------------------------------

@router.get("/{reminder_id}", response_model=ReminderOut)
async def get_reminder(
    pet_id: str,
    reminder_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    doc = await validate_entity_ownership("reminders", reminder_id, pet_id, db)
    today_str, now_hm = await _user_local_clock(current_user["uid"], db)
    return _enrich(doc, today_str, now_hm=now_hm)


# ---------------------------------------------------------------------------
# Update reminder fields (title / date / time / repeat / note)
# ---------------------------------------------------------------------------

@router.patch("/{reminder_id}", response_model=ReminderOut)
async def update_reminder(
    pet_id: str,
    reminder_id: str,
    body: ReminderUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Partial update of reminder data fields.
    This route does NOT touch status — use PATCH .../status for that.
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    existing = await validate_entity_ownership("reminders", reminder_id, pet_id, db)

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise_api_error(422, ErrorCode.NO_FIELDS_TO_UPDATE)

    next_date = updates.get("date", existing.get("date", ""))
    next_time = updates.get("time", existing.get("time", ""))
    if "date" in updates or "time" in updates:
        await _assert_future_datetime(
            current_user["uid"],
            next_date,
            next_time,
            db,
            previous_date=existing.get("date"),
        )
    if "end_date" in updates:
        updates["end_date"] = _normalize_end_date(updates.get("end_date"))
    next_end = updates.get("end_date", existing.get("end_date"))
    _assert_end_after_start(next_date, next_end)

    next_alert = updates.get("alert", existing.get("alert") or "off")
    if next_alert and next_alert != "off":
        tz_name = await _user_timezone_name(current_user["uid"], db)
        alert_ok = is_alert_possible(
            next_date,
            next_time,
            tz_name,
            next_alert,
            datetime.now(timezone.utc),
        )
        if not alert_ok:
            if "alert" in updates:
                raise_api_error(422, ErrorCode.ALERT_NOT_POSSIBLE)
            updates["alert"] = "off"

    await db.reminders.update_one(
        {"_id": ObjectId(reminder_id)}, {"$set": updates}
    )
    updated = await db.reminders.find_one({"_id": ObjectId(reminder_id)})
    today_str, now_hm = await _user_local_clock(current_user["uid"], db)
    return _enrich(updated, today_str, now_hm=now_hm)


# ---------------------------------------------------------------------------
# Update reminder status (completed | missed)
# ---------------------------------------------------------------------------

@router.patch("/{reminder_id}/status", response_model=ReminderOut)
async def update_reminder_status(
    pet_id: str,
    reminder_id: str,
    body: ReminderStatusUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Mark this occurrence as completed or missed.

    The row stays on this date in Recent with that mark. Repeating reminders
    also insert the next date (Upcoming / Today) if it does not exist yet.
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    reminder = await validate_entity_ownership("reminders", reminder_id, pet_id, db)

    uid = current_user["uid"]
    user = await db.users.find_one({"firebase_uid": uid})
    tz_name = (user or {}).get("timezone")
    now = datetime.now(timezone.utc)

    await db.reminders.update_one(
        {"_id": ObjectId(reminder_id)},
        {
            "$set": {
                "status": body.status,
                "notified_at": None,
                "alert_notified_at": None,
            }
        },
    )
    if not reminder.get("next_spawned"):
        await spawn_following_occurrences(db, reminder, tz_name, now)

    updated = await db.reminders.find_one({"_id": ObjectId(reminder_id)})
    today_str, now_hm = await _user_local_clock(uid, db)
    return _enrich(updated, today_str, now_hm=now_hm)

# ---------------------------------------------------------------------------
# Delete reminder
# ---------------------------------------------------------------------------

@router.delete("/{reminder_id}", status_code=204)
async def delete_reminder(
    pet_id: str,
    reminder_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Delete a reminder.
    HealthNotes with linked_reminder_id pointing to this reminder
    are NOT modified — the reference becomes a stale historical link.
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    await validate_entity_ownership("reminders", reminder_id, pet_id, db)
    await db.reminders.delete_one({"_id": ObjectId(reminder_id)})
