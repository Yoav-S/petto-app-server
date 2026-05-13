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
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
from datetime import datetime, timezone

from app.core.database import get_database
from app.core.utils import (
    doc_to_dict,
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


def _enrich(doc: dict) -> ReminderOut:
    """Attach server-computed status to a reminder document."""
    d = doc_to_dict(doc)
    d["status"] = compute_reminder_status(
        d.get("date", ""), d.get("status", "scheduled")
    )
    return ReminderOut(**d)


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
    query = build_reminder_tab_query(pet_id, tab)
    sort_dir = 1 if tab in ("today", "upcoming") else -1
    docs = await db.reminders.find(query, sort=[("date", sort_dir)]).to_list(None)
    return [_enrich(d) for d in docs]


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
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    doc = {
        **body.model_dump(),
        "pet_id": pet_id,
        "status": "scheduled",       # stored status — computed on read
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.reminders.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _enrich(doc)


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
    return _enrich(doc)


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
    await validate_entity_ownership("reminders", reminder_id, pet_id, db)

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    await db.reminders.update_one(
        {"_id": ObjectId(reminder_id)}, {"$set": updates}
    )
    updated = await db.reminders.find_one({"_id": ObjectId(reminder_id)})
    return _enrich(updated)


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
    return _enrich(updated)


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
