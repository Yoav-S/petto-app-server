"""
vaccinations.py — /pets/{pet_id}/vaccinations endpoints.

Key behaviors:
  - `status` is computed on every read from next_date (never stored).
  - Creating a vaccination with next_date automatically creates a Reminder
    with title = "{name} vaccine due", time = "09:00", repeat = "off".
  - Sorted newest first by date.
  - Ownership chain validated: uid → pet_id → vaccination_id.
  - `date` (vaccinated on) cannot be after the user's local today.
  - `next_date` (valid until) cannot be before `date`.
"""
from fastapi import APIRouter, Depends
from app.core.errors import ErrorCode, raise_api_error
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
from datetime import datetime, timezone

from app.core.database import get_database
from app.core.scheduling import resolve_timezone
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


async def _user_today_str(uid: str, db: AsyncIOMotorDatabase) -> str:
    """Return the user's current local date ('YYYY-MM-DD') from their stored timezone."""
    user = await db.users.find_one({"firebase_uid": uid})
    tz = resolve_timezone((user or {}).get("timezone"))
    return datetime.now(tz).date().isoformat()


def _validate_vaccination_dates(
    date: str | None,
    next_date: str | None,
    today_str: str,
) -> None:
    """Enforce vaccinated-on ≤ today and valid-until ≥ vaccinated-on."""
    if date and date > today_str:
        raise_api_error(422, ErrorCode.VACCINATION_DATE_IN_FUTURE)
    if date and next_date and next_date < date:
        raise_api_error(422, ErrorCode.VACCINATION_VALID_UNTIL_BEFORE_DATE)


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
    today_str = await _user_today_str(current_user["uid"], db)
    _validate_vaccination_dates(body.date, body.next_date, today_str)

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
            "notified_at": None,      # set once a push has been sent (dispatcher)
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
    existing = await validate_entity_ownership("vaccinations", vaccination_id, pet_id, db)

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise_api_error(422, ErrorCode.NO_FIELDS_TO_UPDATE)

    today_str = await _user_today_str(current_user["uid"], db)
    merged_date = updates["date"] if "date" in updates else existing.get("date")
    merged_next = updates["next_date"] if "next_date" in updates else existing.get("next_date")
    _validate_vaccination_dates(merged_date, merged_next, today_str)

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
