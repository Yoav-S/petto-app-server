"""
user.py — Pydantic models for the User entity.
"""
from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional, Literal
from datetime import datetime

from app.models.subscription import SubscriptionOut


AuthProvider = Literal["email", "google"]


class UserNameUpdate(BaseModel):
    """Account holder name collected after email verification."""

    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def name_present(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("name")
        return text


class UserOut(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    auth_provider: AuthProvider
    email_verified: bool
    created_at: datetime
    last_login_at: Optional[datetime] = None
    # Source of truth for post-login routing:
    #   has_pets == True  → returning user, go straight into the app
    #   has_pets == False → send to onboarding to add the first pet
    has_pets: bool = False
    subscription: SubscriptionOut = SubscriptionOut()
