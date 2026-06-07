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
