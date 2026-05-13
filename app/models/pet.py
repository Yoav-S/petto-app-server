"""
pet.py — Pydantic models for the Pet entity.

Pet is the root of the ownership chain:
  User → Pet → (Vaccinations, MedicalRecords, Reminders)

All optional fields map directly to what the user fills in on the
Pet Profile screen. photo_url is a Firebase Storage URL; the server
stores only the string (client uploads directly to Firebase Storage).
"""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class PetCreate(BaseModel):
    name: str = Field(..., max_length=100)
    type: str = Field(..., max_length=50)           # e.g. "Dog", "Cat"
    photo_url: Optional[str] = None
    breed: Optional[str] = Field(None, max_length=100)
    birth_date: Optional[str] = None                # "YYYY-MM-DD"
    weight: Optional[float] = None
    chip_id: Optional[str] = Field(None, max_length=100)
    passport_number: Optional[str] = Field(None, max_length=100)
    color: Optional[str] = Field(None, max_length=100)
    is_neutered: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=300)


class PetUpdate(BaseModel):
    """All fields are optional — PATCH updates only what is provided."""
    name: Optional[str] = Field(None, max_length=100)
    type: Optional[str] = Field(None, max_length=50)
    photo_url: Optional[str] = None
    breed: Optional[str] = Field(None, max_length=100)
    birth_date: Optional[str] = None
    weight: Optional[float] = None
    chip_id: Optional[str] = Field(None, max_length=100)
    passport_number: Optional[str] = Field(None, max_length=100)
    color: Optional[str] = Field(None, max_length=100)
    is_neutered: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=300)


class PetOut(BaseModel):
    id: str
    name: str
    type: str
    photo_url: Optional[str]
    breed: Optional[str]
    birth_date: Optional[str]
    weight: Optional[float]
    chip_id: Optional[str]
    passport_number: Optional[str]
    color: Optional[str]
    is_neutered: Optional[bool]
    notes: Optional[str]
    created_at: datetime
