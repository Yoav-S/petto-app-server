"""
pets.py — /pets CRUD endpoints.

Ownership rule: every pet belongs to exactly one Firebase UID.
All reads filter by user_id = current_user["uid"].

DELETE cascades — removing a pet removes all child data:
  vaccinations, medical_records, health_notes, reminders.
"""
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
from datetime import datetime, timezone

from app.core.database import get_database
from app.core.utils import doc_to_dict, validate_pet_ownership
from app.middleware.auth import get_current_user
from app.models.pet import PetCreate, PetUpdate, PetOut

router = APIRouter(prefix="/pets", tags=["pets"])


# ---------------------------------------------------------------------------
# List pets
# ---------------------------------------------------------------------------

@router.get("", response_model=list[PetOut])
async def list_pets(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Return all pets belonging to the authenticated user."""
    docs = await db.pets.find({"user_id": current_user["uid"]}).to_list(None)
    return [PetOut(**doc_to_dict(d)) for d in docs]


# ---------------------------------------------------------------------------
# Create pet
# ---------------------------------------------------------------------------

@router.post("", response_model=PetOut, status_code=201)
async def create_pet(
    body: PetCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Create a new pet for the current user (onboarding + add pet)."""
    doc = {
        **body.model_dump(),
        "user_id": current_user["uid"],
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.pets.insert_one(doc)
    doc["_id"] = result.inserted_id
    return PetOut(**doc_to_dict(doc))


# ---------------------------------------------------------------------------
# Get single pet
# ---------------------------------------------------------------------------

@router.get("/{pet_id}", response_model=PetOut)
async def get_pet(
    pet_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Return a single pet — validates ownership before responding."""
    pet = await validate_pet_ownership(pet_id, current_user["uid"], db)
    return PetOut(**doc_to_dict(pet))


# ---------------------------------------------------------------------------
# Update pet (PATCH — partial update)
# ---------------------------------------------------------------------------

@router.patch("/{pet_id}", response_model=PetOut)
async def update_pet(
    pet_id: str,
    body: PetUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Partial update of pet fields.
    Only fields that are explicitly provided are updated
    (exclude_unset=True prevents overwriting with None).
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    await db.pets.update_one(
        {"_id": ObjectId(pet_id)},
        {"$set": updates},
    )
    updated = await db.pets.find_one({"_id": ObjectId(pet_id)})
    return PetOut(**doc_to_dict(updated))


# ---------------------------------------------------------------------------
# Delete pet (cascade)
# ---------------------------------------------------------------------------

@router.delete("/{pet_id}", status_code=204)
async def delete_pet(
    pet_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Delete a pet and ALL its child data.
    Cascade order: health_notes → medical_records → vaccinations → reminders → pet.
    Returns 204 No Content on success.
    """
    await validate_pet_ownership(pet_id, current_user["uid"], db)

    # Cascade: delete health_notes for all medical_records of this pet
    medical_record_ids = [
        str(doc["_id"])
        async for doc in db.medical_records.find(
            {"pet_id": pet_id}, {"_id": 1}
        )
    ]
    if medical_record_ids:
        await db.health_notes.delete_many(
            {"medical_record_id": {"$in": medical_record_ids}}
        )

    await db.medical_records.delete_many({"pet_id": pet_id})
    await db.vaccinations.delete_many({"pet_id": pet_id})
    await db.reminders.delete_many({"pet_id": pet_id})
    await db.pets.delete_one({"_id": ObjectId(pet_id)})
