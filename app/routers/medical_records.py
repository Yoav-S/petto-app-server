"""
medical_records.py — Health conditions and their nested notes.

Route structure:
  GET    /pets/{pet_id}/medical-records?status=active|resolved
  POST   /pets/{pet_id}/medical-records
  GET    /pets/{pet_id}/medical-records/{id}
  PATCH  /pets/{pet_id}/medical-records/{id}/status
  DELETE /pets/{pet_id}/medical-records/{id}

  POST   /pets/{pet_id}/medical-records/{id}/notes
  PUT    /pets/{pet_id}/medical-records/{id}/notes/{note_id}
  DELETE /pets/{pet_id}/medical-records/{id}/notes/{note_id}

Design notes:
  - MedicalRecord has status "active" | "resolved". Only active→resolved allowed.
  - HealthNote can have optional photo_url and linked_reminder_id.
  - List endpoint enriches each record with latest_note_preview and
    linked_reminder_time for the home screen / list cards.
  - All notes deleted when parent record is deleted (cascade).
"""
import logging

from fastapi import APIRouter, Depends, Query
from app.core.errors import ErrorCode, raise_api_error
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
from datetime import datetime, timezone
from typing import Optional

from app.core.database import get_database
from app.core.utils import (
    doc_to_dict,
    validate_pet_ownership,
    validate_entity_ownership,
    is_valid_object_id,
)
from app.middleware.auth import get_current_user
from app.models.medical_record import (
    MedicalRecordCreate,
    MedicalRecordStatusUpdate,
    MedicalRecordOut,
    MedicalRecordDetailOut,
    HealthNoteCreate,
    HealthNoteUpdate,
    HealthNoteOut,
)

router = APIRouter(prefix="/pets/{pet_id}/medical-records", tags=["medical-records"])
logger = logging.getLogger("petto")


async def _cancel_reminder(reminder_id: Optional[str], db) -> bool:
    """Delete a single linked reminder so it stops firing push notifications."""
    if not reminder_id or not is_valid_object_id(reminder_id):
        return False
    result = await db.reminders.delete_one({"_id": ObjectId(reminder_id)})
    deleted = getattr(result, "deleted_count", 0) or 0
    if deleted:
        logger.info("cancelled reminder %s (linked health note removed/resolved)", reminder_id)
    return bool(deleted)


async def _cancel_reminders_for_record(record_id: str, db) -> int:
    """
    Delete every reminder linked to any note under this record.
    Used when a condition is resolved or deleted so the user stops
    receiving pushes for a health item they've closed out.
    """
    notes = await db.health_notes.find(
        {"medical_record_id": record_id, "linked_reminder_id": {"$ne": None}}
    ).to_list(None)
    cancelled = 0
    for note in notes:
        if await _cancel_reminder(note.get("linked_reminder_id"), db):
            cancelled += 1
    return cancelled


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _build_note_out(note_doc: dict, db) -> HealthNoteOut:
    """
    Convert a raw health_notes document to HealthNoteOut.
    Populates linked_reminder_date and linked_reminder_time if linked.
    """
    d = doc_to_dict(note_doc)
    linked_id = d.get("linked_reminder_id")
    if linked_id and is_valid_object_id(linked_id):
        reminder = await db.reminders.find_one({"_id": ObjectId(linked_id)})
        if reminder:
            d["linked_reminder_date"] = reminder.get("date")
            d["linked_reminder_time"] = reminder.get("time")
    return HealthNoteOut(**d)


async def _get_record_preview(
    record_id: str, db
) -> tuple[
    Optional[str],
    Optional[str],
    Optional[str],
    Optional[str],
    Optional[str],
    Optional[datetime],
]:
    """
    For list cards return:
      latest_note_preview, latest_note_id, latest_note_photo_url,
      linked_reminder_date, linked_reminder_time (only if the latest note has one),
      latest_note_created_at.
    """
    latest = await db.health_notes.find_one(
        {"medical_record_id": record_id},
        sort=[("created_at", -1)],
    )
    if not latest:
        return None, None, None, None, None, None

    preview = latest.get("text", "")[:100]
    latest_note_id = str(latest["_id"])
    latest_note_photo_url = latest.get("photo_url")
    latest_created_at = latest.get("created_at")

    # Reminder on the card only when the *latest* note itself has a linked reminder.
    linked_date = None
    linked_time = None
    linked_id = latest.get("linked_reminder_id")
    if linked_id and is_valid_object_id(linked_id):
        reminder = await db.reminders.find_one({"_id": ObjectId(linked_id)})
        if reminder:
            linked_date = reminder.get("date")
            linked_time = reminder.get("time")

    return preview, latest_note_id, latest_note_photo_url, linked_date, linked_time, latest_created_at


async def _enrich_record(doc: dict, db) -> MedicalRecordOut:
    """Build MedicalRecordOut with preview fields for list cards."""
    d = doc_to_dict(doc)
    record_id = d["id"]
    preview, latest_note_id, latest_photo, linked_date, linked_time, latest_created_at = (
        await _get_record_preview(record_id, db)
    )
    d["latest_note_preview"] = preview
    d["latest_note_id"] = latest_note_id
    d["latest_note_photo_url"] = latest_photo
    d["linked_reminder_date"] = linked_date
    d["linked_reminder_time"] = linked_time
    d["updated_at"] = latest_created_at or d.get("resolved_at") or d.get("created_at")
    return MedicalRecordOut(**d)


# ---------------------------------------------------------------------------
# List medical records
# ---------------------------------------------------------------------------

@router.get("", response_model=list[MedicalRecordOut])
async def list_medical_records(
    pet_id: str,
    status: str = Query("active", pattern="^(active|resolved)$"),
    limit: Optional[int] = Query(None, ge=1, le=50, description="Page size (omit for all)."),
    cursor: Optional[str] = Query(None, description="Pass the last item's id to get the next page."),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Return medical records (conditions) filtered by status, newest first.

    Pagination (health main screen):
      - Pass `limit` (e.g. 5) to page. The response is an array; there are more
        pages when you receive exactly `limit` items — use the last item's `id`
        as `cursor` for the next request.
      - Omit `limit` to get everything (used by the home summary).
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    query = {"pet_id": pet_id, "status": status}
    if cursor and is_valid_object_id(cursor):
        query["_id"] = {"$lt": ObjectId(cursor)}
    docs = await db.medical_records.find(query, sort=[("_id", -1)]).to_list(limit or None)
    if limit:
        docs = docs[:limit]
    return [await _enrich_record(d, db) for d in docs]


# ---------------------------------------------------------------------------
# Create medical record (health condition)
# ---------------------------------------------------------------------------

@router.post("", response_model=MedicalRecordOut, status_code=201)
async def create_medical_record(
    pet_id: str,
    body: MedicalRecordCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Create a new health condition tracker."""
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    doc = {
        "pet_id": pet_id,
        "title": body.title,
        "status": "active",
        "created_at": datetime.now(timezone.utc),
        "resolved_at": None,
    }
    result = await db.medical_records.insert_one(doc)
    doc["_id"] = result.inserted_id
    return await _enrich_record(doc, db)


# ---------------------------------------------------------------------------
# Get single medical record (with all notes)
# ---------------------------------------------------------------------------

@router.get("/{record_id}", response_model=MedicalRecordDetailOut)
async def get_medical_record(
    pet_id: str,
    record_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Return a condition with all its health notes, newest first."""
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    record = await validate_entity_ownership("medical_records", record_id, pet_id, db)

    note_docs = await db.health_notes.find(
        {"medical_record_id": record_id},
        sort=[("created_at", -1)],
    ).to_list(None)

    notes = [await _build_note_out(n, db) for n in note_docs]
    d = doc_to_dict(record)
    d["notes"] = notes
    preview, latest_note_id, latest_photo, linked_date, linked_time, latest_created_at = (
        await _get_record_preview(record_id, db)
    )
    d["latest_note_preview"] = preview
    d["latest_note_id"] = latest_note_id
    d["latest_note_photo_url"] = latest_photo
    d["linked_reminder_date"] = linked_date
    d["linked_reminder_time"] = linked_time
    d["updated_at"] = latest_created_at or d.get("resolved_at") or d.get("created_at")
    return MedicalRecordDetailOut(**d)


# ---------------------------------------------------------------------------
# List notes for a record (paginated timeline)
# ---------------------------------------------------------------------------

@router.get("/{record_id}/notes", response_model=list[HealthNoteOut])
async def list_record_notes(
    pet_id: str,
    record_id: str,
    limit: Optional[int] = Query(None, ge=1, le=50, description="Page size (e.g. 5)."),
    cursor: Optional[str] = Query(None, description="Pass the last note's id for the next (older) page."),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Return a condition's notes newest-first, for the timeline screen.

    Pagination: pass `limit` (e.g. 5); there are older notes when you receive
    exactly `limit` items — use the last note's `id` as `cursor` to load older ones.
    Each note includes linked_reminder_date/time when a reminder is attached.
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    await validate_entity_ownership("medical_records", record_id, pet_id, db)

    query = {"medical_record_id": record_id}
    if cursor and is_valid_object_id(cursor):
        query["_id"] = {"$lt": ObjectId(cursor)}
    docs = await db.health_notes.find(query, sort=[("_id", -1)]).to_list(limit or None)
    if limit:
        docs = docs[:limit]
    return [await _build_note_out(n, db) for n in docs]


# ---------------------------------------------------------------------------
# Update medical record status (active → resolved)
# ---------------------------------------------------------------------------

@router.patch("/{record_id}/status", response_model=MedicalRecordOut)
async def update_medical_record_status(
    pet_id: str,
    record_id: str,
    body: MedicalRecordStatusUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Mark a health condition as resolved.
    Only active → resolved is allowed; resolved conditions cannot be re-opened.
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    record = await validate_entity_ownership("medical_records", record_id, pet_id, db)

    if record.get("status") == "resolved":
        raise_api_error(422, ErrorCode.ALREADY_RESOLVED)

    now = datetime.now(timezone.utc)
    await db.medical_records.update_one(
        {"_id": ObjectId(record_id)},
        {"$set": {"status": "resolved", "resolved_at": now}},
    )
    # Resolved health items should stop nagging the user — cancel linked reminders.
    await _cancel_reminders_for_record(record_id, db)
    updated = await db.medical_records.find_one({"_id": ObjectId(record_id)})
    return await _enrich_record(updated, db)


# ---------------------------------------------------------------------------
# Delete medical record (cascade notes)
# ---------------------------------------------------------------------------

@router.delete("/{record_id}", status_code=204)
async def delete_medical_record(
    pet_id: str,
    record_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Delete a condition and all its health notes (and their linked reminders)."""
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    await validate_entity_ownership("medical_records", record_id, pet_id, db)
    # Cancel reminders first (needs the notes to still exist to read their links).
    await _cancel_reminders_for_record(record_id, db)
    await db.health_notes.delete_many({"medical_record_id": record_id})
    await db.medical_records.delete_one({"_id": ObjectId(record_id)})


# ===========================================================================
# HealthNote sub-routes
# ===========================================================================

@router.post("/{record_id}/notes", response_model=HealthNoteOut, status_code=201)
async def add_note(
    pet_id: str,
    record_id: str,
    body: HealthNoteCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Add a health note to a condition.
    If linked_reminder_id is provided, validate it belongs to the same pet.
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    await validate_entity_ownership("medical_records", record_id, pet_id, db)

    # Validate linked reminder belongs to same pet
    if body.linked_reminder_id:
        await validate_entity_ownership(
            "reminders", body.linked_reminder_id, pet_id, db
        )

    doc = {
        "medical_record_id": record_id,
        "text": body.text,
        "photo_url": body.photo_url,
        "linked_reminder_id": body.linked_reminder_id,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.health_notes.insert_one(doc)
    doc["_id"] = result.inserted_id
    return await _build_note_out(doc, db)


@router.put("/{record_id}/notes/{note_id}", response_model=HealthNoteOut)
async def update_note(
    pet_id: str,
    record_id: str,
    note_id: str,
    body: HealthNoteUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Update a health note.
    Pass `photo_url: null` to remove the photo.
    Pass `linked_reminder_id: null` to unlink the reminder.
    Only explicitly provided fields are updated.
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    await validate_entity_ownership("medical_records", record_id, pet_id, db)
    await validate_entity_ownership(
        "health_notes", note_id, record_id, db, parent_field="medical_record_id"
    )

    updates = body.model_dump(exclude_unset=True)

    # Validate new linked_reminder_id if being set (not null)
    new_linked = updates.get("linked_reminder_id")
    if new_linked is not None:
        await validate_entity_ownership("reminders", new_linked, pet_id, db)

    if not updates:
        raise_api_error(422, ErrorCode.NO_FIELDS_TO_UPDATE)

    await db.health_notes.update_one(
        {"_id": ObjectId(note_id)}, {"$set": updates}
    )
    updated = await db.health_notes.find_one({"_id": ObjectId(note_id)})
    return await _build_note_out(updated, db)


@router.delete("/{record_id}/notes/{note_id}", status_code=204)
async def delete_note(
    pet_id: str,
    record_id: str,
    note_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Delete a single health note and cancel its linked reminder (stop stale pushes)."""
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    await validate_entity_ownership("medical_records", record_id, pet_id, db)
    note = await validate_entity_ownership(
        "health_notes", note_id, record_id, db, parent_field="medical_record_id"
    )
    await _cancel_reminder(note.get("linked_reminder_id"), db)
    await db.health_notes.delete_one({"_id": ObjectId(note_id)})
