"""
vaccination.py — Pydantic models for the Vaccination entity.

Key behaviors:
  - `status` is server-computed from next_date; never stored or accepted from client.
  - Creating a vaccination with next_date → auto-creates a Reminder (handled in router).
  - Sorting: newest first (by date).
  - `photo_url` is a Firebase Storage URL (client uploads, server stores URL only).
"""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class VaccinationCreate(BaseModel):
    name: str = Field(..., max_length=200)
    date: str                                        # "YYYY-MM-DD" — vaccinated on (≤ today)
    next_date: Optional[str] = None                  # "YYYY-MM-DD" — valid until (≥ date)
    note: Optional[str] = Field(None, max_length=300)
    photo_url: Optional[str] = None                  # proof photo URL (Firebase Storage)
    vet_clinic: Optional[str] = Field(None, max_length=300)


class VaccinationUpdate(BaseModel):
    """
    Partial update. Pass `null` explicitly to clear photo_url, vet_clinic, or next_date.
    """
    name: Optional[str] = Field(None, max_length=200)
    date: Optional[str] = None
    next_date: Optional[str] = None
    note: Optional[str] = Field(None, max_length=300)
    photo_url: Optional[str] = None
    vet_clinic: Optional[str] = Field(None, max_length=300)


class VaccinationOut(BaseModel):
    id: str
    pet_id: str
    name: str
    date: str
    next_date: Optional[str] = None
    note: Optional[str] = None
    photo_url: Optional[str] = None
    vet_clinic: Optional[str] = None
    status: str          # "up_to_date" | "due_soon" | "overdue" — server-computed
    created_at: datetime
