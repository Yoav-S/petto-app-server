"""
notifications.py — Push token registration + reminder dispatch.

Endpoints (all under /api/v1):
  POST /notifications/register     app registers its Expo push token + timezone (auth)
  POST /notifications/unregister   app removes a token on logout (auth)
  POST /internal/dispatch-reminders  send all due reminders (secret-protected)

The dispatcher is meant to be called on a schedule (Cloud Scheduler, ~every
minute). It is also fully usable by hand from Swagger /docs for testing:
  - ?dry_run=true returns exactly which reminders are due and what WOULD be sent,
    without sending anything or touching the DB.
  - a real call returns the Expo delivery result per reminder.

All activity is logged via logger "petto" -> visible in Cloud Run logs.
"""
import hmac
import logging
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.core.database import get_database
from app.core.push import is_dead_token_ticket, send_expo_push
from app.core.scheduling import compute_scheduled_at
from app.core.utils import is_valid_object_id
from app.middleware.auth import get_current_user
from app.models.notification import (
    NotificationPrefs,
    NotificationPrefsUpdate,
    RegisterPushRequest,
    RegisterPushResponse,
    UnregisterPushRequest,
)

router = APIRouter(tags=["notifications"])
logger = logging.getLogger("petto")

# All switches default ON — a fresh user receives everything until they opt out.
DEFAULT_NOTIFICATION_PREFS = {
    "all": True,
    "reminders": True,
    "vaccine_updates": True,
    "health_reminders": True,
    "email_updates": True,
}


def _merge_notification_prefs(stored: dict | None) -> NotificationPrefs:
    """Overlay stored switches on the defaults so older users get sane values."""
    data = {**DEFAULT_NOTIFICATION_PREFS, **(stored or {})}
    return NotificationPrefs(**{key: data[key] for key in DEFAULT_NOTIFICATION_PREFS})


# ---------------------------------------------------------------------------
# App-facing: register / unregister a device
# ---------------------------------------------------------------------------

@router.post("/notifications/register", response_model=RegisterPushResponse)
async def register_push(
    body: RegisterPushRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """
    Store the device's Expo push token and/or timezone for the current user.
    Safe to call on every app launch — it's an idempotent upsert.
    """
    uid = current_user["uid"]
    now = datetime.now(timezone.utc)

    timezone_saved = False
    if body.timezone:
        await db.users.update_one(
            {"firebase_uid": uid},
            {"$set": {"timezone": body.timezone, "updated_at": now}},
        )
        timezone_saved = True

    token_saved = False
    if body.token:
        await db.push_tokens.update_one(
            {"token": body.token},
            {
                "$set": {
                    "token": body.token,
                    "user_id": uid,
                    "platform": body.platform,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        token_saved = True

    logger.info(
        "push register uid=%s token_saved=%s tz_saved=%s", uid, token_saved, timezone_saved
    )
    return RegisterPushResponse(
        ok=True, token_saved=token_saved, timezone_saved=timezone_saved
    )


@router.post("/notifications/unregister", status_code=204)
async def unregister_push(
    body: UnregisterPushRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Remove a push token (e.g. on logout). Only affects the caller's own token."""
    await db.push_tokens.delete_one({"token": body.token, "user_id": current_user["uid"]})


# ---------------------------------------------------------------------------
# App-facing: notification preferences
# ---------------------------------------------------------------------------

@router.get("/notifications/preferences", response_model=NotificationPrefs)
async def get_notification_preferences(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Return the current user's notification switches (defaults when unset)."""
    user = await db.users.find_one({"firebase_uid": current_user["uid"]})
    return _merge_notification_prefs((user or {}).get("notification_prefs"))


@router.patch("/notifications/preferences", response_model=NotificationPrefs)
async def update_notification_preferences(
    body: NotificationPrefsUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Persist changed switches; returns the full, merged preference set."""
    uid = current_user["uid"]
    changes = body.model_dump(exclude_unset=True)

    if changes:
        now = datetime.now(timezone.utc)
        updates = {f"notification_prefs.{key}": value for key, value in changes.items()}
        updates["updated_at"] = now
        await db.users.update_one({"firebase_uid": uid}, {"$set": updates})
        logger.info("notification prefs updated uid=%s changes=%s", uid, changes)

    user = await db.users.find_one({"firebase_uid": uid})
    return _merge_notification_prefs((user or {}).get("notification_prefs"))


# ---------------------------------------------------------------------------
# Internal: reminder dispatcher (secret-protected, called by Cloud Scheduler)
# ---------------------------------------------------------------------------

async def require_internal_secret(
    x_internal_secret: str | None = Header(default=None),
) -> None:
    """Guard the dispatch endpoint with a shared secret (constant-time compare)."""
    secret = settings.INTERNAL_TASK_SECRET
    if not secret:
        # Disabled until a secret is configured — never leave it open by default.
        raise HTTPException(status_code=503, detail="dispatch disabled: set INTERNAL_TASK_SECRET")
    if not x_internal_secret or not hmac.compare_digest(x_internal_secret, secret):
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("/internal/dispatch-reminders")
async def dispatch_reminders(
    dry_run: bool = Query(False, description="Preview due reminders without sending or writing."),
    _: None = Depends(require_internal_secret),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Find every reminder whose local date/time has arrived and push it."""
    now = datetime.now(timezone.utc)

    candidates = await db.reminders.find(
        {
            "status": "scheduled",
            "$or": [{"notified_at": None}, {"notified_at": {"$exists": False}}],
        }
    ).to_list(None)

    pet_cache: dict[str, dict | None] = {}
    tz_cache: dict[str, str | None] = {}
    token_cache: dict[str, list[str]] = {}
    prefs_cache: dict[str, NotificationPrefs] = {}

    async def get_pet(pet_id: str) -> dict | None:
        if pet_id not in pet_cache:
            pet_cache[pet_id] = (
                await db.pets.find_one({"_id": ObjectId(pet_id)})
                if is_valid_object_id(pet_id)
                else None
            )
        return pet_cache[pet_id]

    async def get_tz(uid: str) -> str | None:
        if uid not in tz_cache:
            user = await db.users.find_one({"firebase_uid": uid})
            tz_cache[uid] = (user or {}).get("timezone")
        return tz_cache[uid]

    async def get_tokens(uid: str) -> list[str]:
        if uid not in token_cache:
            docs = await db.push_tokens.find({"user_id": uid}).to_list(None)
            token_cache[uid] = [d["token"] for d in docs if d.get("token")]
        return token_cache[uid]

    async def get_prefs(uid: str) -> NotificationPrefs:
        if uid not in prefs_cache:
            user = await db.users.find_one({"firebase_uid": uid})
            prefs_cache[uid] = _merge_notification_prefs((user or {}).get("notification_prefs"))
        return prefs_cache[uid]

    items: list[dict] = []
    due_count = 0
    processed = 0

    for reminder in candidates:
        pet = await get_pet(reminder.get("pet_id", ""))
        if not pet:
            continue
        uid = pet.get("user_id")
        scheduled_at = compute_scheduled_at(
            reminder.get("date", ""), reminder.get("time", ""), await get_tz(uid)
        )
        if not scheduled_at or scheduled_at > now:
            continue

        due_count += 1
        tokens = await get_tokens(uid)
        prefs = await get_prefs(uid)
        # Master switch + the "Reminders" category both gate reminder pushes.
        reminders_enabled = prefs.all and prefs.reminders
        reminder_id = str(reminder["_id"])
        item = {
            "reminder_id": reminder_id,
            "pet_id": reminder.get("pet_id"),
            "title": reminder.get("title"),
            "scheduled_at": scheduled_at.isoformat(),
            "tokens": len(tokens),
        }

        if dry_run:
            item["would_send"] = bool(tokens) and reminders_enabled
            if not reminders_enabled:
                item["reason"] = "notifications_disabled"
            items.append(item)
            continue

        if not reminders_enabled:
            # User opted out — skip silently and leave the reminder unmarked so
            # it resumes firing if they re-enable notifications.
            item["delivered"] = False
            item["reason"] = "notifications_disabled"
            items.append(item)
            continue

        if not tokens:
            # Nothing to deliver yet (e.g. still on Expo Go). Leave it unmarked so
            # it fires once the user has a real build + token.
            item["delivered"] = False
            item["reason"] = "no_tokens"
            items.append(item)
            logger.info("reminder %s due but user %s has no push tokens", reminder_id, uid)
            continue

        messages = [
            {
                "to": token,
                "title": pet.get("name") or "Petto reminder",
                "body": reminder.get("title", "Reminder"),
                "sound": "default",
                "data": {
                    "type": "reminder",
                    "reminderId": reminder_id,
                    "petId": reminder.get("pet_id"),
                },
            }
            for token in tokens
        ]
        tickets = await send_expo_push(messages)

        for token, ticket in zip(tokens, tickets):
            if is_dead_token_ticket(ticket):
                await db.push_tokens.delete_one({"token": token})
                logger.info("pruned dead push token for user %s", uid)

        # Mark as notified but keep the occurrence date until the user taps
        # Done / Missed. Recurring rollover happens in PATCH .../status so the
        # client can still prompt for this fire.
        await db.reminders.update_one(
            {"_id": reminder["_id"]},
            {"$set": {"notified_at": now}},
        )

        processed += 1
        item["delivered"] = any(t.get("status") == "ok" for t in tickets)
        items.append(item)

    summary = {
        "now_utc": now.isoformat(),
        "candidates": len(candidates),
        "due": due_count,
        "processed": processed,
        "dry_run": dry_run,
        "items": items,
    }
    logger.info(
        "dispatch-reminders candidates=%d due=%d processed=%d dry_run=%s",
        len(candidates),
        due_count,
        processed,
        dry_run,
    )
    return summary
