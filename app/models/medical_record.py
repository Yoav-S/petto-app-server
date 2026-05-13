"""
medical_record.py — Pydantic models for MedicalRecord and HealthNote.

Architecture (from screen analysis):
  MedicalRecord = a health condition (e.g. "Allergy", "Ear infection")
    └── HealthNote  = a time-stamped observation within that condition

HealthNote can optionally carry:
  - photo_url       : Firebase Storage URL (client uploads, server stores URL)
  - linked_reminder_id : reference to an existing Reminder

MedicalRecord.status flows in one direction only: active → resolved.
"""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


# ---------------------------------------------------------------------------
# HealthNote
# ---------------------------------------------------------------------------

class HealthNoteCreate(BaseModel):
    text: str = Field(..., max_length=300)
    photo_url: Optional[str] = None
    linked_reminder_id: Optional[str] = None     # must belong to same pet


class HealthNoteUpdate(BaseModel):
    """
    All fields optional. Pass `null` / None explicitly to clear photo or reminder.
    e.g. {"photo_url": null} removes the photo.
    """
    text: Optional[str] = Field(None, max_length=300)
    photo_url: Optional[str] = None
    linked_reminder_id: Optional[str] = None


class HealthNoteOut(BaseModel):
    id: str
    medical_record_id: str
    text: str
    photo_url: Optional[str]
    linked_reminder_id: Optional[str]
    # Flattened reminder display fields (populated when linked_reminder_id is set)
    linked_reminder_date: Optional[str] = None   # "YYYY-MM-DD"
    linked_reminder_time: Optional[str] = None   # "HH:MM"
    created_at: datetime


# ---------------------------------------------------------------------------
# MedicalRecord
# ---------------------------------------------------------------------------

class MedicalRecordCreate(BaseModel):
    title: str = Field(..., max_length=300)      # free-text condition name


class MedicalRecordStatusUpdate(BaseModel):
    """Used by PATCH .../status — only transition allowed is active → resolved."""
    status: str = Field(..., pattern="^resolved$")


class MedicalRecordOut(BaseModel):
    id: str
    pet_id: str
    title: str
    status: str                                  # "active" | "resolved"
    created_at: datetime
    resolved_at: Optional[datetime]
    # Preview fields for list cards (populated server-side)
    latest_note_preview: Optional[str] = None   # first 100 chars of latest note
    linked_reminder_time: Optional[str] = None  # "HH:MM" from latest note's reminder


class MedicalRecordDetailOut(MedicalRecordOut):
    """Full detail view includes all notes, newest first."""
    notes: list[HealthNoteOut] = []
