"""
subscription.py — Plan helpers and free-tier limits.
"""
from datetime import datetime, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

FREE_MAX_PETS = 1
FREE_MAX_ACTIVE_REMINDERS = 10

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


async def count_active_reminders(
    uid: str,
    db: AsyncIOMotorDatabase,
    today_str: str,
) -> int:
    """
    Active = stored status scheduled and date >= today (today + upcoming tabs).
    Counted across all of the user's pets.
    """
    pet_ids: list[str] = []
    async for pet in db.pets.find({"user_id": uid}, {"_id": 1}):
        pet_ids.append(str(pet["_id"]))
    if not pet_ids:
        return 0
    return await db.reminders.count_documents(
        {
            "pet_id": {"$in": pet_ids},
            "status": "scheduled",
            "date": {"$gte": today_str},
        }
    )
