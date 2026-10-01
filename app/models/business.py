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


class GeoLocation(BaseModel):
    """GeoJSON point. coordinates are [longitude, latitude]."""

    type: Literal["Point"] = "Point"
    coordinates: list[float] = Field(min_length=2, max_length=2)

    @field_validator("coordinates")
    @classmethod
    def coordinates_in_range(cls, value: list[float]) -> list[float]:
        longitude, latitude = value
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise ValueError("coordinates")
        return [float(longitude), float(latitude)]


class OpeningHours(BaseModel):
    always_open: bool = False
    mon: list[TimeSlot] = Field(default_factory=list)
    tue: list[TimeSlot] = Field(default_factory=list)
    wed: list[TimeSlot] = Field(default_factory=list)
    thu: list[TimeSlot] = Field(default_factory=list)
    fri: list[TimeSlot] = Field(default_factory=list)
    sat: list[TimeSlot] = Field(default_factory=list)
    sun: list[TimeSlot] = Field(default_factory=list)


class BusinessSubmit(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone: list[str] = Field(min_length=1)
    email: Optional[str] = None
    description: Optional[str] = None
    category: BusinessCategory
    city: str = Field(min_length=1, max_length=120)
    address: str = Field(min_length=1, max_length=300)
    timezone: str = Field(min_length=1, max_length=64)
    opening_hours: OpeningHours
    website: Optional[str] = None
    location: GeoLocation
    photo: Optional[str] = None
    instagram: Optional[str] = None

    @field_validator("phone")
    @classmethod
    def phone_present(cls, value: list[str]) -> list[str]:
        cleaned = [phone.strip() for phone in value if phone and phone.strip()]
        if not cleaned:
            raise ValueError("phone")
        return cleaned


_REVIEW_FIELDS = frozenset(
    {
        "name",
        "phone",
        "email",
        "description",
        "category",
        "city",
        "address",
        "timezone",
        "hours",
        "website",
        "location",
        "photo",
        "instagram",
    }
)


class BusinessReject(BaseModel):
    field_errors: dict[str, str]

    @field_validator("field_errors")
    @classmethod
    def notes_present(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for key, message in value.items():
            if key not in _REVIEW_FIELDS:
                continue
            text = message.strip()
            if text:
                cleaned[key] = text[:500]
        if not cleaned:
            raise ValueError("field_errors")
        return cleaned


class AdminPublish(BusinessSubmit):
    """A listing an admin publishes for an owner who asked by phone."""

    owner_email: str = Field(min_length=3, max_length=320)


class InvitationOut(BaseModel):
    id: str
    business_id: str
    business_name: str = ""
    email: str
    role: Literal["owner", "worker"]
    status: Literal["pending", "approved", "declined"]


class BusinessOut(BaseModel):
    id: str
    name: str
    phone: list[str]
    email: Optional[str] = None
    description: Optional[str] = None
    category: BusinessCategory
    city: str
    status: BusinessStatus
    address: str
    timezone: str
    opening_hours: OpeningHours
    website: Optional[str] = None
    location: Optional[GeoLocation] = None
    photo: Optional[str] = None
    photos: list[str] = Field(default_factory=list)
    owner_uid: Optional[str] = None
    owner_email: Optional[str] = None
    owned: bool = False
    instagram: Optional[str] = None
    rejection_reason: Optional[str] = None
    field_errors: dict[str, str] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    invitations: list[InvitationOut] = Field(default_factory=list)


class PhotoRemove(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


class OwnerInvite(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class TeamInvite(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: Literal["owner", "worker"]


class BusinessSession(BaseModel):
    is_ragly_admin: bool
    business: Optional[BusinessOut] = None
    role: Optional[Literal["owner", "worker"]] = None
    invitations: list[InvitationOut] = Field(default_factory=list)
