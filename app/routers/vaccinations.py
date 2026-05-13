"""
vaccinations.py — /pets/{pet_id}/vaccinations endpoints.

Key behaviors:
  - `status` is computed on every read from next_date (never stored).
  - Creating a vaccination with next_date automatically creates a Reminder
    with title = "{name} vaccine due", time = "09:00", repeat = "off".
  - Sorted newest first by date.
  - Ownership chain validated: uid → pet_id → vaccination_id.
"""
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
from datetime import datetime, timezone

from app.core.database import get_database
from app.core.utils import (
    doc_to_dict,
    validate_pet_ownership,
    validate_entity_ownership,
    compute_vaccination_status,
)
from app.middleware.auth import get_current_user
from app.models.vaccination import VaccinationCreate, VaccinationUpdate, VaccinationOut

router = APIRouter(prefix="/pets/{pet_id}/vaccinations", tags=["vaccinations"])


def _enrich(doc: dict) -> VaccinationOut:
    """Attach computed status to a vaccination document."""
    d = doc_to_dict(doc)
    d["status"] = compute_vaccination_status(d.get("next_date"))
    return VaccinationOut(**d)


# ---------------------------------------------------------------------------
# List vaccinations
# ---------------------------------------------------------------------------

@router.get("", response_model=list[VaccinationOut])
async def list_vaccinations(
    pet_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Return all vaccinations for a pet, newest first."""
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    docs = await db.vaccinations.find(
        {"pet_id": pet_id}, sort=[("date", -1)]
    ).to_list(None)
    return [_enrich(d) for d in docs]


# ---------------------------------------------------------------------------
# Create vaccination
# ---------------------------------------------------------------------------

@router.post("", response_model=VaccinationOut, status_code=201)
async def create_vaccination(
    pet_id: str,
    body: VaccinationCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Create a vaccination record.
    If next_date is provided → auto-create a linked Reminder (server-rules §3.2).
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)

    doc = {
        **body.model_dump(),
        "pet_id": pet_id,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.vaccinations.insert_one(doc)
    doc["_id"] = result.inserted_id

    # Auto-create reminder when next_date is set (server-rules §3.2)
    if body.next_date:
        reminder_doc = {
            "pet_id": pet_id,
            "title": f"{body.name} vaccine due",
            "date": body.next_date,
            "time": "09:00",          # default time (server-rules §4)
            "repeat": "off",
            "status": "scheduled",
            "note": None,
            "created_at": datetime.now(timezone.utc),
        }
        await db.reminders.insert_one(reminder_doc)

    return _enrich(doc)


# ---------------------------------------------------------------------------
# Get single vaccination
# ---------------------------------------------------------------------------

@router.get("/{vaccination_id}", response_model=VaccinationOut)
async def get_vaccination(
    pet_id: str,
    vaccination_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    doc = await validate_entity_ownership("vaccinations", vaccination_id, pet_id, db)
    return _enrich(doc)


# ---------------------------------------------------------------------------
# Update vaccination
# ---------------------------------------------------------------------------

@router.patch("/{vaccination_id}", response_model=VaccinationOut)
async def update_vaccination(
    pet_id: str,
    vaccination_id: str,
    body: VaccinationUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    await validate_entity_ownership("vaccinations", vaccination_id, pet_id, db)

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    await db.vaccinations.update_one(
        {"_id": ObjectId(vaccination_id)}, {"$set": updates}
    )
    updated = await db.vaccinations.find_one({"_id": ObjectId(vaccination_id)})
    return _enrich(updated)


# ---------------------------------------------------------------------------
# Delete vaccination
# ---------------------------------------------------------------------------

@router.delete("/{vaccination_id}", status_code=204)
async def delete_vaccination(
    pet_id: str,
    vaccination_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    await validate_pet_ownership(pet_id, current_user["uid"], db)
    await validate_entity_ownership("vaccinations", vaccination_id, pet_id, db)
    await db.vaccinations.delete_one({"_id": ObjectId(vaccination_id)})
