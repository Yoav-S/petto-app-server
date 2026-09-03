"""
subscription.py — Plan helpers and free-tier limits.
"""
from datetime import datetime, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

FREE_MAX_PETS = 1
FREE_MAX_ACTIVE_REMINDERS = 50

DEFAULT_SUBSCRIPTION: dict[str, Any] = {
    "plan": "free",
    "provider": None,
    "product_id": None,
    "expires_at": None,
    "will_renew": False,
    "updated_at": None,
}


def normalize_subscription(raw: Optional[dict]) -> dict[str, Any]:
    """Merge stored subscription with defaults (existing users have none)."""
    base = dict(DEFAULT_SUBSCRIPTION)
    if not raw or not isinstance(raw, dict):
        return base
    base.update({k: raw.get(k, base[k]) for k in base})
    return base


def user_has_premium(user_doc: Optional[dict], *, now: Optional[datetime] = None) -> bool:
    """True when plan is premium and not expired."""
    if not user_doc:
        return False
    sub = normalize_subscription(user_doc.get("subscription"))
    if sub.get("plan") != "premium":
        return False
    expires = sub.get("expires_at")
    if expires is None:
        return True
    if isinstance(expires, str):
        try:
            expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    check = now or datetime.now(timezone.utc)
    return expires > check


async def count_user_pets(uid: str, db: AsyncIOMotorDatabase) -> int:
    return await db.pets.count_documents({"user_id": uid})


async def get_included_pet_id(uid: str, db: AsyncIOMotorDatabase) -> Optional[str]:
    """Oldest pet stays usable on the free plan after a downgrade."""
    pet = await db.pets.find_one(
        {"user_id": uid},
        sort=[("created_at", 1), ("_id", 1)],
        projection={"_id": 1},
    )
    return str(pet["_id"]) if pet else None


async def is_pet_locked_for_owner(
    pet: dict,
    db: AsyncIOMotorDatabase,
    *,
    user_doc: Optional[dict] = None,
) -> bool:
    """True when a free-plan owner cannot open this pet."""
    uid = pet.get("user_id")
    if not uid:
        return True
    user = user_doc if user_doc is not None else await db.users.find_one({"firebase_uid": uid})
    if user_has_premium(user):
        return False
    included = await get_included_pet_id(uid, db)
    return bool(included) and str(pet["_id"]) != included


async def count_active_reminders(
    uid: str,
    db: AsyncIOMotorDatabase,
    today_str: str,
    *,
    pet_ids: Optional[list[str]] = None,
) -> int:
    """
    Active = stored status scheduled and date >= today (today + upcoming tabs).
    Counted in Mongo. Free plan counts only the included pet so locked pets
    cannot block adding reminders on the pet the user can still use.
    """
    ids = pet_ids
    if ids is None:
        ids = []
        async for pet in db.pets.find({"user_id": uid}, {"_id": 1}):
            ids.append(str(pet["_id"]))
    if not ids:
        return 0
    return await db.reminders.count_documents(
        {
            "pet_id": {"$in": ids},
            "status": "scheduled",
            "date": {"$gte": today_str},
        }
    )


async def can_add_active_reminder(
    uid: str,
    db: AsyncIOMotorDatabase,
    today_str: str,
    *,
    user_doc: Optional[dict] = None,
) -> bool:
    user = user_doc if user_doc is not None else await db.users.find_one({"firebase_uid": uid})
    if user_has_premium(user):
        return True
    included = await get_included_pet_id(uid, db)
    if not included:
        return True
    active = await count_active_reminders(uid, db, today_str, pet_ids=[included])
    return active < FREE_MAX_ACTIVE_REMINDERS
