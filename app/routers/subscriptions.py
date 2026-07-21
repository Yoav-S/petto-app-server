"""
subscriptions.py — RevenueCat webhook + plan mirroring.

POST /subscriptions/webhook  — public (Bearer secret), updates user.subscription
"""
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.core.database import get_database
from app.core.errors import ErrorCode, raise_api_error
from app.core.subscription import DEFAULT_SUBSCRIPTION, normalize_subscription
from app.models.subscription import RevenueCatWebhook

logger = logging.getLogger("petto")

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

ENTITLEMENT_ID = "petto_premium"

# Events that (re)grant access while entitlement is active.
GRANT_EVENTS = {
    "INITIAL_PURCHASE",
    "RENEWAL",
    "UNCANCELLATION",
    "NON_RENEWING_PURCHASE",
    "PRODUCT_CHANGE",
    "SUBSCRIPTION_EXTENDED",
    "TEMPORARY_ENTITLEMENT_GRANT",
}

REVOKE_EVENTS = {
    "EXPIRATION",
}

# Still entitled until expires_at — only flip will_renew.
SOFT_CANCEL_EVENTS = {
    "CANCELLATION",
}


def _ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _has_premium_entitlement(entitlement_ids: list[str]) -> bool:
    return ENTITLEMENT_ID in entitlement_ids or not entitlement_ids


async def _find_user_by_app_user_id(db: AsyncIOMotorDatabase, app_user_id: str):
    if not app_user_id:
        return None
    return await db.users.find_one({"firebase_uid": app_user_id})


async def _set_subscription(db: AsyncIOMotorDatabase, uid: str, patch: dict[str, Any]) -> None:
    sub = {**DEFAULT_SUBSCRIPTION, **patch, "updated_at": datetime.now(timezone.utc)}
    await db.users.update_one(
        {"firebase_uid": uid},
        {"$set": {"subscription": sub, "updated_at": datetime.now(timezone.utc)}},
    )


@router.post("/webhook", status_code=200)
async def revenuecat_webhook(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_database),
    authorization: Optional[str] = Header(default=None),
):
    """
    RevenueCat server notification. Auth: Authorization: Bearer <REVENUECAT_WEBHOOK_SECRET>.
    App User ID must be the Firebase UID (Purchases.logIn(uid) on the client).
    """
    secret = settings.REVENUECAT_WEBHOOK_SECRET.strip()
    if not secret:
        logger.warning("RevenueCat webhook called but REVENUECAT_WEBHOOK_SECRET is empty")
        raise_api_error(503, ErrorCode.GENERIC)

    expected = f"Bearer {secret}"
    if (authorization or "").strip() != expected:
        raise_api_error(401, ErrorCode.UNAUTHORIZED)

    try:
        payload = await request.json()
        body = RevenueCatWebhook.model_validate(payload)
    except Exception:
        logger.exception("Invalid RevenueCat webhook payload")
        raise_api_error(400, ErrorCode.GENERIC)

    event = body.event
    event_type = (event.type or "").upper()
    app_user_id = (event.app_user_id or "").strip()

    logger.info(
        "RevenueCat webhook type=%s user=%s product=%s",
        event_type,
        app_user_id,
        event.product_id,
    )

    if event_type == "TRANSFER":
        # Move premium from transferred_from → transferred_to (first ids).
        from_ids = event.transferred_from or []
        to_ids = event.transferred_to or []
        if from_ids and to_ids:
            source = await _find_user_by_app_user_id(db, from_ids[0])
            target_uid = to_ids[0]
            if source and await _find_user_by_app_user_id(db, target_uid):
                sub = normalize_subscription(source.get("subscription"))
                await _set_subscription(db, target_uid, sub)
                await _set_subscription(
                    db,
                    from_ids[0],
                    {**DEFAULT_SUBSCRIPTION, "plan": "free"},
                )
        return {"ok": True}

    user = await _find_user_by_app_user_id(db, app_user_id)
    if not user:
        # RC may send anonymous ids before logIn; acknowledge so RC does not retry forever.
        logger.warning("RevenueCat webhook: no user for app_user_id=%s", app_user_id)
        return {"ok": True, "ignored": True}

    expires_at = _ms_to_dt(event.expiration_at_ms)
    product_id = event.product_id

    if event_type in REVOKE_EVENTS:
        await _set_subscription(
            db,
            app_user_id,
            {
                "plan": "free",
                "provider": "revenuecat",
                "product_id": product_id,
                "expires_at": expires_at,
                "will_renew": False,
            },
        )
        return {"ok": True}

    if event_type in SOFT_CANCEL_EVENTS:
        existing = normalize_subscription(user.get("subscription"))
        await _set_subscription(
            db,
            app_user_id,
            {
                **existing,
                "provider": "revenuecat",
                "product_id": product_id or existing.get("product_id"),
                "expires_at": expires_at or existing.get("expires_at"),
                "will_renew": False,
                # Keep plan premium until EXPIRATION.
                "plan": existing.get("plan") or "premium",
            },
        )
        return {"ok": True}

    if event_type in GRANT_EVENTS:
        if not _has_premium_entitlement(event.entitlement_ids):
            return {"ok": True, "ignored": True}
        await _set_subscription(
            db,
            app_user_id,
            {
                "plan": "premium",
                "provider": "revenuecat",
                "product_id": product_id,
                "expires_at": expires_at,
                "will_renew": True,
            },
        )
        return {"ok": True}

    # TEST / unknown — acknowledge
    logger.info("RevenueCat webhook unhandled type=%s", event_type)
    return {"ok": True, "ignored": True}
