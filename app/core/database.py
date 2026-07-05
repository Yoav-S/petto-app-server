"""
database.py — MongoDB connection management.

Uses Motor (async) with a single shared client.
Indexes are created on startup to enforce query performance
on the most common lookup paths: user_id, pet_id, date.
"""
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from app.core.config import settings

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


async def connect_to_db() -> None:
    """Open Motor client and create collection indexes."""
    global _client, _db
    _client = AsyncIOMotorClient(settings.MONGODB_URI)
    _db = _client[settings.mongodb_db_name]

    # users — lookup by Firebase UID or email
    await _db.users.create_index("firebase_uid", unique=True, sparse=True)
    await _db.users.create_index("email", unique=True)

    # email OTPs — one active OTP per email
    await _db.email_otps.create_index("email", unique=True)
    await _db.email_otps.create_index("expires_at", expireAfterSeconds=0)
    await _db.pets.create_index("user_id")

    # vaccinations — look up by pet, sort by date
    await _db.vaccinations.create_index("pet_id")
    await _db.vaccinations.create_index([("pet_id", 1), ("date", -1)])

    # medical_records — look up by pet + status
    await _db.medical_records.create_index([("pet_id", 1), ("status", 1)])

    # health_notes — look up by parent record, sort newest first
    await _db.health_notes.create_index([("medical_record_id", 1), ("created_at", -1)])

    # reminders — look up by pet + date (for tab filtering)
    await _db.reminders.create_index([("pet_id", 1), ("date", 1)])
    await _db.reminders.create_index([("pet_id", 1), ("status", 1)])
    # reminders — dispatcher scan for un-notified scheduled reminders
    await _db.reminders.create_index([("status", 1), ("notified_at", 1)])

    # push_tokens — one document per device token, look up by user
    await _db.push_tokens.create_index("token", unique=True)
    await _db.push_tokens.create_index("user_id")


async def close_db_connection() -> None:
    """Close the Motor client on shutdown."""
    global _client
    if _client:
        _client.close()


def get_database() -> AsyncIOMotorDatabase:
    """FastAPI dependency — returns the active database handle."""
    if _db is None:
        raise RuntimeError("Database is not connected")
    return _db
