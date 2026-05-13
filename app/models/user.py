"""
user.py — Pydantic models for the User entity.

Users are created automatically on first login (upsert).
The user document stores the Firebase UID as the primary identifier
so that every ownership check user_id → pet_id is Firebase-native.
"""
from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime


class UserCreate(BaseModel):
    email: EmailStr


class UserOut(BaseModel):
    id: str
    email: str
    created_at: datetime
