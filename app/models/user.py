"""
user.py — Pydantic models for the User entity.
"""
from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional, Literal
from datetime import datetime


OnboardingStep = Literal["name", "type", "photo", "birth"]
PetKind = Literal["dog", "cat"]

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


class UserProfileUpdate(UserNameUpdate):
    """Name is required. Phone and photo are optional and can be cleared."""

    phone: Optional[str] = Field(default=None, max_length=32)
    photo_url: Optional[str] = None

    @field_validator("phone")
    @classmethod
    def phone_clean(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("photo_url")
    @classmethod
    def photo_https(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if not text.startswith("https://"):
            raise ValueError("photo_url")
        return text


class EmailChangeRequest(BaseModel):
    email: EmailStr


class EmailChangeConfirm(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=6, max_length=6)


class OnboardingProgress(BaseModel):
    """In-progress first pet. Cleared once that pet is created."""

    step: OnboardingStep = "name"
    pet_name: Optional[str] = Field(default=None, max_length=40)
    pet_type: Optional[PetKind] = None
    photo_url: Optional[str] = None
    birth_date: Optional[str] = None

    @field_validator("pet_name")
    @classmethod
    def pet_name_clean(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("photo_url")
    @classmethod
    def photo_https(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if not text.startswith("https://"):
            raise ValueError("photo_url")
        return text

    @field_validator("birth_date")
    @classmethod
    def birth_iso(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("birth_date") from exc
        return text


def coherent_onboarding(progress: OnboardingProgress) -> OnboardingProgress:
    """A later step is only resumable when the earlier required answers exist."""
    step = progress.step
    if step != "name" and not progress.pet_name:
        step = "name"
    elif step in ("photo", "birth") and not progress.pet_type:
        step = "type"
    if step == progress.step:
        return progress
    return progress.model_copy(update={"step": step})


class UserOut(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    phone: Optional[str] = None
    photo_url: Optional[str] = None
    auth_provider: AuthProvider
    email_verified: bool
    created_at: datetime
    last_login_at: Optional[datetime] = None
    # Source of truth for post-login routing:
    #   has_pets == True  → returning user, go straight into the app
    #   has_pets == False → send to onboarding to add the first pet
    has_pets: bool = False
    # Absent once the first pet exists. New accounts resume this step.
    onboarding: Optional[OnboardingProgress] = None
    subscription: SubscriptionOut = SubscriptionOut()
