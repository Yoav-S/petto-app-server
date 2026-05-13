"""
vaccination.py — Pydantic models for the Vaccination entity.

Key behaviors:
  - `status` is server-computed from next_date; never stored or accepted from client.
  - Creating a vaccination with next_date → auto-creates a Reminder (handled in router).
  - Sorting: newest first (by date).
"""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class VaccinationCreate(BaseModel):
    name: str = Field(..., max_length=200)
    date: str                                        # "YYYY-MM-DD" — past date
    next_date: Optional[str] = None                  # "YYYY-MM-DD" — future date
    note: Optional[str] = Field(None, max_length=300)


class VaccinationUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    date: Optional[str] = None
    next_date: Optional[str] = None
    note: Optional[str] = Field(None, max_length=300)


class VaccinationOut(BaseModel):
    id: str
    pet_id: str
    name: str
    date: str
    next_date: Optional[str]
    note: Optional[str]
    status: str          # "up_to_date" | "due_soon" | "overdue" — server-computed
    created_at: datetime
