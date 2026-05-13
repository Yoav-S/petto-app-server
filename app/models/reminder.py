"""
reminder.py — Pydantic models for the Reminder entity.

Key design decisions (from screen analysis):
  - `type` field: DROPPED. Reminders use free-text `title` only.
  - `repeat` enum matches exactly the options shown in the Figma repeat picker.
  - `time` is stored separately from `date` as "HH:MM" string.
  - `status` returned by API is server-computed; stored_status in DB is
    one of: "scheduled" | "completed" | "missed".
    The API returns the richer set: "today" | "scheduled" | "missed" | "completed".
  - No auto-record creation when completed (no type field to drive it).
"""
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime

# Exact repeat options from the Figma repeat picker
ReminderRepeat = Literal[
    "off",
    "every_day",
    "every_2_days",
    "every_week",
    "every_2_weeks",
    "every_month",
    "every_year",
]


class ReminderCreate(BaseModel):
    title: str = Field(..., max_length=300)
    date: str                                     # "YYYY-MM-DD"
    time: str = Field(..., pattern=r"^\d{2}:\d{2}$")  # "HH:MM"
    repeat: ReminderRepeat = "off"
    note: Optional[str] = Field(None, max_length=300)


class ReminderUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=300)
    date: Optional[str] = None
    time: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    repeat: Optional[ReminderRepeat] = None
    note: Optional[str] = Field(None, max_length=300)


class ReminderStatusUpdate(BaseModel):
    """
    Used by PATCH .../status.
    Client sends "completed" (Done button) or "missed" (Missed button).
    """
    status: Literal["completed", "missed"]


class ReminderOut(BaseModel):
    id: str
    pet_id: str
    title: str
    date: str
    time: str
    repeat: str
    note: Optional[str]
    # Server-computed display status: "today" | "scheduled" | "missed" | "completed"
    status: str
    created_at: datetime
