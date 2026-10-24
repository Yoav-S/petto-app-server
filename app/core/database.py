"""
database.py — MongoDB connection management.

Uses Motor (async) with a single shared client.
Indexes are created on startup to enforce query performance
on the most common lookup paths: user_id, pet_id, date.
"""
import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import settings
from app.core.reminder_series import (
    backfill_missing_series_ids,
    split_duplicate_series_dates,
)

logger = logging.getLogger("petto")

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
    await _ensure_reminder_series_date_index(_db)

    # push_tokens — one document per device token, look up by user
    await _db.push_tokens.create_index("token", unique=True)
    await _db.push_tokens.create_index("user_id")

    indexes = await _db.businesses.index_information()
    owner_index = indexes.get("owner_uid_1")
    # One person can own several businesses. Drop the old one-business unique index.
    if owner_index and owner_index.get("unique"):
        await _db.businesses.drop_index("owner_uid_1")
        owner_index = None
    if owner_index is None:
        await _db.businesses.create_index("owner_uid")
    await _db.businesses.create_index("status")
    await _ensure_business_name_index(_db)
    member_indexes = await _db.business_members.index_information()
    if "business_id_1_user_id_1" in member_indexes:
        await _db.business_members.drop_index("business_id_1_user_id_1")
    await _db.business_members.create_index(
        [("business_id", 1), ("user_id", 1), ("location_id", 1)],
        unique=True,
    )
    await _db.business_locations.create_index("business_id")
    await _db.business_locations.create_index("status")
    await _db.business_locations.create_index("services.status")
    await _db.business_events.create_index([("business_id", 1), ("created_at", -1)])
    await _db.business_notice_reads.create_index("user_id", unique=True)
    await _migrate_business_branches(_db)
    await _db.business_invitations.create_index(
        [("business_id", 1), ("email", 1), ("status", 1)]
    )
    await _db.business_invitations.create_index("email")
    await _db.business_reviews.create_index(
        [("business_id", 1), ("user_id", 1)],
        unique=True,
    )
    await _db.business_reviews.create_index("business_id")


async def _ensure_reminder_series_date_index(db) -> None:
    """Unique (series_id, date) without crashing on legacy null series_id."""
    filled = await backfill_missing_series_ids(db)
    split = await split_duplicate_series_dates(db)
    if filled or split:
        logger.info(
            "Reminder series index prep: backfilled=%s split_collisions=%s",
            filled,
            split,
        )
    try:
        await db.reminders.drop_index("series_id_1_date_1")
    except Exception:
        pass
    try:
        await db.reminders.create_index(
            [("series_id", 1), ("date", 1)],
            unique=True,
            name="series_id_1_date_1",
            partialFilterExpression={"series_id": {"$type": "string"}},
        )
    except Exception:
        logger.exception(
            "Could not create unique series_id+date index; continuing startup"
        )


async def _ensure_business_name_index(db) -> None:
    """One registered name. An older duplicate keeps a suffixed key so the index can be created."""
    from app.business_names import business_name_key

    seen: dict[str, object] = {}
    cursor = db.businesses.find({}, {"name": 1, "name_key": 1})
    async for doc in cursor:
        key = business_name_key(str(doc.get("name") or "")) or f"unnamed-{doc['_id']}"
        if key in seen and seen[key] != doc["_id"]:
            logger.warning("Duplicate business name left in place for %s", doc["_id"])
            key = f"{key}#{doc['_id']}"
        else:
            seen[key] = doc["_id"]
        if doc.get("name_key") != key:
            await db.businesses.update_one({"_id": doc["_id"]}, {"$set": {"name_key": key}})
    try:
        await db.businesses.create_index("name_key", unique=True, name="business_name_key")
    except Exception:
        logger.exception("Could not create unique business name index; continuing startup")


async def _migrate_business_branches(db) -> None:
    """Bring every business and branch to the current document shape.

    Runs on each start and changes nothing once documents are current:
    - a business that still carries its address itself gets one branch row;
    - a branch without services gets one service from the business category
      and the branch hours; missing flags (is_main, visible, status) are filled;
    - the business mirrors its main branch (category, categories, address, hours).
    """
    from datetime import datetime, timezone

    from app import business_docs as docs

    now = datetime.now(timezone.utc)
    migrated_businesses = 0
    migrated_branches = 0
    cursor = db.businesses.find({})
    async for business in cursor:
        rows = await docs.list_branches(db, str(business["_id"]))
        changed = False
        if not rows:
            legacy = docs.legacy_branch(business, now)
            if legacy is not None:
                result = await db.business_locations.insert_one(legacy)
                legacy["_id"] = result.inserted_id
                rows = [legacy]
                changed = True
                migrated_branches += 1
        for row in rows:
            update = docs.branch_upgrade(row, business, now)
            if update:
                await db.business_locations.update_one({"_id": row["_id"]}, {"$set": update})
                row.update(update)
                changed = True
                migrated_branches += 1
        if rows and not any(row.get("is_main") for row in rows):
            main = docs.main_branch(rows)
            if main is not None:
                await db.business_locations.update_one({"_id": main["_id"]}, {"$set": {"is_main": True}})
                main["is_main"] = True
                changed = True
        mirrored = docs.mirror_fields(business, rows) if rows else {}
        needs_keys = any(key not in business for key in ("categories", "locale", "field_errors", "admin_notes", "photos", "visible"))
        stale_mirror = any(business.get(key) != value for key, value in mirrored.items())
        if changed or needs_keys or stale_mirror:
            fill = {
                "categories": mirrored.get("categories", []),
                "locale": business.get("locale") if business.get("locale") in {"en", "ro", "ru", "he"} else "en",
                "field_errors": docs.notes(business.get("field_errors")),
                "admin_notes": docs.notes(business.get("admin_notes")),
                "photos": docs.gallery(business),
                "status": docs.business_status(business),
                "visible": bool(business.get("visible", True)),
                **mirrored,
            }
            if "source" not in business:
                fill["source"] = "admin" if not business.get("owner_uid") else "owner"
            await db.businesses.update_one({"_id": business["_id"]}, {"$set": fill})
            migrated_businesses += 1
    if migrated_businesses or migrated_branches:
        logger.info(
            "Business migration: businesses=%s branches=%s", migrated_businesses, migrated_branches
        )


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
