"""
utils.py — Shared helpers used across routers.

Keeps all status calculation logic in one place so it's easy
to find and test in isolation. Also holds the MongoDB document
serializer so no router needs to know about _id / ObjectId.
"""
from datetime import date, timedelta
from typing import Any, Optional
from bson import ObjectId
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def doc_to_dict(doc: dict) -> dict:
    """
    Convert a raw MongoDB document to a plain dict suitable for API responses.
    - Renames _id → id
    - Serializes ObjectId values to strings
    """
    if doc is None:
        return {}
    result = {}
    for key, value in doc.items():
        if key == "_id":
            result["id"] = str(value)
        elif isinstance(value, ObjectId):
            result[key] = str(value)
        else:
            result[key] = value
    return result


def is_valid_object_id(value: str) -> bool:
    """Return True if value is a valid MongoDB ObjectId string."""
    return ObjectId.is_valid(value)


# ---------------------------------------------------------------------------
# Ownership validation
# ---------------------------------------------------------------------------

async def validate_pet_ownership(pet_id: str, uid: str, db) -> dict:
    """
    Confirm that pet_id exists and belongs to uid.
    Returns the raw pet document.
    Raises HTTP 404 (not 403) — we never reveal whether a resource exists
    to an unauthorized caller.
    """
    if not is_valid_object_id(pet_id):
        raise HTTPException(status_code=404, detail="Not found")
    pet = await db.pets.find_one({"_id": ObjectId(pet_id)})
    if not pet or pet.get("user_id") != uid:
        raise HTTPException(status_code=404, detail="Not found")
    return pet


async def validate_entity_ownership(
    collection_name: str,
    entity_id: str,
    pet_id: str,
    db,
    parent_field: str = "pet_id",
) -> dict:
    """
    Confirm that entity_id exists in collection and belongs to pet_id.
    Returns the raw entity document.
    """
    if not is_valid_object_id(entity_id):
        raise HTTPException(status_code=404, detail="Not found")
    entity = await db[collection_name].find_one({"_id": ObjectId(entity_id)})
    if not entity or entity.get(parent_field) != pet_id:
        raise HTTPException(status_code=404, detail="Not found")
    return entity


# ---------------------------------------------------------------------------
# Status calculators — server is source of truth (server-rules §3.3)
# ---------------------------------------------------------------------------

def compute_vaccination_status(next_date_str: Optional[str]) -> str:
    """
    Compute vaccination display status from next_date string ("YYYY-MM-DD").

    Rules:
      - No next_date         → up_to_date  (no booster tracking)
      - next_date > today+30 → up_to_date
      - next_date ≤ today+30 → due_soon
      - next_date < today    → overdue
    """
    if not next_date_str:
        return "up_to_date"
    try:
        nxt = date.fromisoformat(next_date_str)
    except ValueError:
        return "up_to_date"
    today = date.today()
    if nxt < today:
        return "overdue"
    if nxt <= today + timedelta(days=30):
        return "due_soon"
    return "up_to_date"


def compute_reminder_status(reminder_date_str: str, stored_status: str) -> str:
    """
    Compute the reminder status to return in API responses.

    stored_status comes from the DB and is one of:
      "scheduled" | "completed" | "missed"

    Returned status (what the client sees):
      "today" | "scheduled" | "missed" | "completed"

    Logic:
      - completed / missed → always return stored value (user explicitly set these)
      - scheduled + date < today → auto "missed" (date passed without action)
      - scheduled + date == today → "today"
      - scheduled + date > today → "scheduled"
    """
    if stored_status in ("completed", "missed"):
        return stored_status
    today_str = date.today().isoformat()
    if reminder_date_str < today_str:
        return "missed"
    if reminder_date_str == today_str:
        return "today"
    return "scheduled"


def build_reminder_tab_query(pet_id: str, tab: str) -> dict:
    """
    Build a MongoDB filter dict for the reminders tab endpoint.

    today    → date == today, stored_status == "scheduled"
    upcoming → date >  today, stored_status == "scheduled"
    recent   → stored_status in [completed, missed]
               OR (date < today AND status == "scheduled")  ← auto-missed
    """
    today_str = date.today().isoformat()
    base = {"pet_id": pet_id}

    if tab == "today":
        return {**base, "date": today_str, "status": "scheduled"}

    if tab == "upcoming":
        return {**base, "date": {"$gt": today_str}, "status": "scheduled"}

    # tab == "recent"
    return {
        **base,
        "$or": [
            {"status": {"$in": ["completed", "missed"]}},
            {"date": {"$lt": today_str}, "status": "scheduled"},
        ],
    }
