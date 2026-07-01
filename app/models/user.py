"""
user.py — Pydantic models for the User entity.
"""
from pydantic import BaseModel, EmailStr
from typing import Optional, Literal
from datetime import datetime


AuthProvider = Literal["email", "google"]


class UserOut(BaseModel):
    id: str
    email: str
    auth_provider: AuthProvider
    email_verified: bool
    created_at: datetime
    last_login_at: Optional[datetime] = None
    # Source of truth for post-login routing:
    #   has_pets == True  → returning user, go straight into the app
    #   has_pets == False → send to onboarding to add the first pet
    has_pets: bool = False
