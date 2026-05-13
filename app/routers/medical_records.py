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
from fastapi import APIRouter, Depends, HTTPException, Query
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


async def _get_record_preview(record_id: str, db) -> tuple[Optional[str], Optional[str]]:
    """
    For list cards: return (latest_note_preview, linked_reminder_time).
    Fetches the single most recent note for this record.
    """
    latest = await db.health_notes.find_one(
        {"medical_record_id": record_id},
        sort=[("created_at", -1)],
    )
    if not latest:
        return None, None

    preview = latest.get("text", "")[:100]
    linked_time = None
    linked_id = latest.get("linked_reminder_id")
    if linked_id and is_valid_object_id(linked_id):
        reminder = await db.reminders.find_one({"_id": ObjectId(linked_id)})
        if reminder:
            linked_time = reminder.get("time")
    return preview, linked_time


async def _enrich_record(doc: dict, db) -> MedicalRecordOut:
    """Build MedicalRecordOut with preview fields for list cards."""
    d = doc_to_dict(doc)
    record_id = d["id"]
    preview, linked_time = await _get_record_preview(record_id, db)
    d["latest_note_preview"] = preview
    d["linked_reminder_time"] = linked_time
    return MedicalRecordOut(**d)


# ---------------------------------------------------------------------------
# List medical records
# ---------------------------------------------------------------------------

@router.get("", response_model=list[MedicalRecordOut])
async def list_medical_records(
    pet_id: str,
    status: str = Query("active", pattern="^(active|resolved)$"),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Return medical records filtered by status.
    Each record includes latest_note_preview and linked_reminder_time
    so the client can render list cards without extra requests.
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    docs = await db.medical_records.find(
        {"pet_id": pet_id, "status": status},
        sort=[("created_at", -1)],
    ).to_list(None)
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
    d["latest_note_preview"] = notes[0].text[:100] if notes else None
    d["linked_reminder_time"] = notes[0].linked_reminder_time if notes else None
    return MedicalRecordDetailOut(**d)


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
        raise HTTPException(status_code=422, detail="Already resolved")

    now = datetime.now(timezone.utc)
    await db.medical_records.update_one(
        {"_id": ObjectId(record_id)},
        {"$set": {"status": "resolved", "resolved_at": now}},
    )
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
    """Delete a condition and all its health notes."""
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    await validate_entity_ownership("medical_records", record_id, pet_id, db)
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
        raise HTTPException(status_code=422, detail="No fields to update")

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
    """Delete a single health note. Does not delete the linked reminder."""
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    await validate_entity_ownership("medical_records", record_id, pet_id, db)
    await validate_entity_ownership(
        "health_notes", note_id, record_id, db, parent_field="medical_record_id"
    )
    await db.health_notes.delete_one({"_id": ObjectId(note_id)})
