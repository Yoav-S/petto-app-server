"""
subscription models — API + webhook shapes for billing.
"""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


PlanName = Literal["free", "premium"]


class SubscriptionOut(BaseModel):
    plan: PlanName = "free"
    provider: Optional[str] = None
    product_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    will_renew: bool = False
    updated_at: Optional[datetime] = None


class RevenueCatEvent(BaseModel):
    """Subset of RevenueCat webhook event fields we care about."""

    type: str
    app_user_id: str = ""
    original_app_user_id: Optional[str] = None
    product_id: Optional[str] = None
    entitlement_ids: list[str] = Field(default_factory=list)
    expiration_at_ms: Optional[int] = None
    purchased_at_ms: Optional[int] = None
    store: Optional[str] = None
    environment: Optional[str] = None
    transferred_from: list[str] = Field(default_factory=list)
    transferred_to: list[str] = Field(default_factory=list)


class RevenueCatWebhook(BaseModel):
    api_version: Optional[str] = None
    event: RevenueCatEvent
