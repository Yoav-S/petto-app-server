"""
notification.py — Pydantic models for push-notification endpoints.
"""
from pydantic import BaseModel, Field
from typing import Optional


class RegisterPushRequest(BaseModel):
    """
    Sent by the app after login.

    - token: Expo push token (e.g. "ExponentPushToken[...]"). Omitted in Expo Go,
      where a token cannot be generated — the timezone is still stored so reminders
      schedule correctly once the app runs as a real build.
    - platform: "ios" | "android" (informational).
    - timezone: device IANA timezone (e.g. "Asia/Jerusalem"), used to fire
      reminders at the user's local date/time.
    """
    token: Optional[str] = Field(None, max_length=255)
    platform: Optional[str] = Field(None, max_length=20)
    timezone: Optional[str] = Field(None, max_length=64)


class RegisterPushResponse(BaseModel):
    ok: bool
    token_saved: bool
    timezone_saved: bool


class UnregisterPushRequest(BaseModel):
    token: str = Field(..., max_length=255)


class NotificationPrefs(BaseModel):
    """
    Per-user notification switches (stored on the user document under
    `notification_prefs`). `all` is the master gate — when False, nothing is
    delivered regardless of the category switches.
    """
    all: bool = True
    reminders: bool = True
    vaccine_updates: bool = True
    health_reminders: bool = True
    email_updates: bool = True


class NotificationPrefsUpdate(BaseModel):
    """Partial update — only provided switches are changed."""
    all: Optional[bool] = None
    reminders: Optional[bool] = None
    vaccine_updates: Optional[bool] = None
    health_reminders: Optional[bool] = None
    email_updates: Optional[bool] = None
