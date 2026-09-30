"""Business listing submitted by an owner and reviewed by a Ragly admin."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

BusinessCategory = Literal[
    "veterinarian",
    "groomer",
    "pharmacy",
    "pet_friendly",
    "pet_store",
]
BusinessStatus = Literal[
    "pending_review",
    "published",
    "rejected",
    "suspended",
]


class TimeSlot(BaseModel):
    open: str
    close: str


class OpeningHours(BaseModel):
    always_open: bool
    mon: list[TimeSlot] = Field(default_factory=list)
    tue: list[TimeSlot] = Field(default_factory=list)
    wed: list[TimeSlot] = Field(default_factory=list)
    thu: list[TimeSlot] = Field(default_factory=list)
    fri: list[TimeSlot] = Field(default_factory=list)
    sat: list[TimeSlot] = Field(default_factory=list)
    sun: list[TimeSlot] = Field(default_factory=list)


class BusinessSubmit(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phones: list[str] = Field(min_length=1)
    email: Optional[str] = None
    description: Optional[str] = None
    category: BusinessCategory
    city: str = Field(min_length=1, max_length=120)
    address: str = Field(min_length=1, max_length=300)
    timezone: str = Field(min_length=1, max_length=64)
    opening_hours: OpeningHours
    website: Optional[str] = None
    latitude: float
    longitude: float
    photo_url: Optional[str] = None
    instagram: Optional[str] = None

    @field_validator("phones")
    @classmethod
    def phones_present(cls, value: list[str]) -> list[str]:
        cleaned = [phone.strip() for phone in value if phone and phone.strip()]
        if not cleaned:
            raise ValueError("phones")
        return cleaned


class BusinessReject(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class AdminPublish(BusinessSubmit):
    """A listing an admin publishes for an owner who asked by phone."""

    owner_email: str = Field(min_length=3, max_length=320)


class BusinessOut(BaseModel):
    id: str
    owner_uid: Optional[str] = None
    owner_email: str
    name: str
    phones: list[str]
    email: Optional[str] = None
    description: Optional[str] = None
    category: BusinessCategory
    city: str
    status: BusinessStatus
    address: str
    timezone: str
    opening_hours: OpeningHours
    website: Optional[str] = None
    latitude: float
    longitude: float
    photo_url: Optional[str] = None
    instagram: Optional[str] = None
    rejection_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    submitted_at: Optional[datetime] = None


class BusinessSession(BaseModel):
    is_ragly_admin: bool
    business: Optional[BusinessOut] = None
