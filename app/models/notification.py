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
