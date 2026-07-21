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
from app.core.errors import ErrorCode, raise_api_error
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
from datetime import datetime, timezone

from app.core.database import get_database
from app.core.scheduling import resolve_timezone, compute_scheduled_at
from app.core.subscription import (
    FREE_MAX_ACTIVE_REMINDERS,
    count_active_reminders,
    user_has_premium,
)
from app.core.utils import (
    doc_to_dict,
    is_valid_object_id,
    validate_pet_ownership,
    validate_entity_ownership,
    compute_reminder_status,
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


async def _assert_unique_datetime(
    pet_id: str,
    date: str,
    time: str,
    db: AsyncIOMotorDatabase,
    *,
    exclude_reminder_id: str | None = None,
) -> None:
    """Reject another scheduled reminder on the same pet at the same local date+time."""
    query: dict = {
        "pet_id": pet_id,
        "date": date,
        "time": time,
        "status": "scheduled",
    }
    if exclude_reminder_id and is_valid_object_id(exclude_reminder_id):
        query["_id"] = {"$ne": ObjectId(exclude_reminder_id)}
    if await db.reminders.find_one(query):
        raise_api_error(409, ErrorCode.DUPLICATE_REMINDER_DATETIME)


async def _assert_future_datetime(
    uid: str,
    date: str,
    time: str,
    db: AsyncIOMotorDatabase,
) -> None:
    """Reject reminders whose local date+time is already in the past."""
    user = await db.users.find_one({"firebase_uid": uid})
    tz_name = (user or {}).get("timezone")
    scheduled_at = compute_scheduled_at(date, time, tz_name)
    if not scheduled_at:
        raise_api_error(422, ErrorCode.REMINDER_DATETIME_IN_PAST)
    if scheduled_at <= datetime.now(timezone.utc):
        raise_api_error(422, ErrorCode.REMINDER_DATETIME_IN_PAST)


def _enrich(doc: dict, today_str: str | None = None) -> ReminderOut:
    """Attach server-computed status to a reminder document (in the user's tz)."""
    d = doc_to_dict(doc)
    d["status"] = compute_reminder_status(
        d.get("date", ""), d.get("status", "scheduled"), today_str
    )
    return ReminderOut(**d)


async def _user_today_str(uid: str, db: AsyncIOMotorDatabase) -> str:
    """Return the user's current local date ('YYYY-MM-DD') from their stored timezone."""
    user = await db.users.find_one({"firebase_uid": uid})
    tz = resolve_timezone((user or {}).get("timezone"))
    return datetime.now(tz).date().isoformat()


# ---------------------------------------------------------------------------
# List reminders (tab filtering)
# ---------------------------------------------------------------------------

@router.get("", response_model=list[ReminderOut])
async def list_reminders(
    pet_id: str,
    tab: str = Query("today", pattern="^(today|upcoming|recent)$"),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Return reminders for a tab.
    Sorting:
      today/upcoming → soonest first (date ASC)
      recent         → most recent first (date DESC)
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    today_str = await _user_today_str(current_user["uid"], db)
    query = build_reminder_tab_query(pet_id, tab, today_str)
    sort_dir = 1 if tab in ("today", "upcoming") else -1
    docs = await db.reminders.find(query, sort=[("date", sort_dir)]).to_list(None)
    return [_enrich(d, today_str) for d in docs]


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
    if not user_has_premium(user):
        today_str = await _user_today_str(uid, db)
        active = await count_active_reminders(uid, db, today_str)
        if active >= FREE_MAX_ACTIVE_REMINDERS:
            raise_api_error(403, ErrorCode.PREMIUM_REQUIRED_REMINDER)

    await _assert_future_datetime(uid, body.date, body.time, db)
    await _assert_unique_datetime(pet_id, body.date, body.time, db)
    doc = {
        **body.model_dump(),
        "pet_id": pet_id,
        "status": "scheduled",       # stored status — computed on read
        "notified_at": None,         # set once a push has been sent (dispatcher)
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.reminders.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _enrich(doc, await _user_today_str(uid, db))


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
    return _enrich(doc, await _user_today_str(current_user["uid"], db))


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
        await _assert_future_datetime(current_user["uid"], next_date, next_time, db)
        await _assert_unique_datetime(
            pet_id,
            next_date,
            next_time,
            db,
            exclude_reminder_id=reminder_id,
        )

    await db.reminders.update_one(
        {"_id": ObjectId(reminder_id)}, {"$set": updates}
    )
    updated = await db.reminders.find_one({"_id": ObjectId(reminder_id)})
    return _enrich(updated, await _user_today_str(current_user["uid"], db))


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
    Mark a reminder as completed or missed.
    This is a separate route from field updates to keep intent clear.
    Note: no auto-record creation (type field was dropped).
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    await validate_entity_ownership("reminders", reminder_id, pet_id, db)

    await db.reminders.update_one(
        {"_id": ObjectId(reminder_id)},
        {"$set": {"status": body.status}},
    )
    updated = await db.reminders.find_one({"_id": ObjectId(reminder_id)})
    return _enrich(updated, await _user_today_str(current_user["uid"], db))


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
