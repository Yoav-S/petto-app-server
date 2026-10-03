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
    """A listing an admin publishes. An owner email is optional."""

    owner_email: Optional[str] = None

    @field_validator("owner_email")
    @classmethod
    def owner_email_optional(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip().lower()
        if not text:
            return None
        if "@" not in text or "." not in text.split("@")[-1]:
            raise ValueError("owner_email")
        return text


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


class BusinessCounts(BaseModel):
    pending: int = 0
    published: int = 0
    rejected: int = 0
    deleted: int = 0


class BusinessPage(BaseModel):
    items: list[BusinessOut]
    has_more: bool = False


class BusinessPlace(BaseModel):
    """A published listing shown to pet owners, closest first when a location is sent."""

    id: str
    name: str
    category: BusinessCategory
    city: str
    image: Optional[str] = None
    distance_km: Optional[float] = None
    rating: Optional[float] = None
    open_now: bool = False
    open_24_7: bool = False
    closes_at: Optional[str] = None
    opens_at: Optional[str] = None
    next_open_day: Optional[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = None
    opens_tomorrow: bool = False


class BusinessPlacePage(BaseModel):
    items: list[BusinessPlace]
    has_more: bool = False


class PlaceReview(BaseModel):
    """A published review row for the business screen. Comment is optional."""

    id: str
    author_name: str = ""
    author_photo: Optional[str] = None
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None
    created_at: datetime
    is_mine: bool = False


class ReviewWrite(BaseModel):
    """One rating from the signed-in pet owner. The comment can be empty."""

    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("comment")
    @classmethod
    def comment_clean(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        return text or None


class BusinessPlaceDetail(BusinessPlace):
    """Published listing plus the fields the business screen shows."""

    description: Optional[str] = None
    address: str = ""
    phone: list[str] = Field(default_factory=list)
    website: Optional[str] = None
    instagram: Optional[str] = None
    opening_hours: OpeningHours = Field(default_factory=OpeningHours)
    location: Optional[GeoLocation] = None
    reviews: list[PlaceReview] = Field(default_factory=list)


class BusinessReview(BaseModel):
    """One pet owner's rating of a business they used.

    Stored in business_reviews, not on the business document.
    One review per user per business. No photos.
    """

    id: str
    business_id: str
    user_id: str
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class BusinessSession(BaseModel):
    is_ragly_admin: bool
    business: Optional[BusinessOut] = None
    role: Optional[Literal["owner", "worker"]] = None
    invitations: list[InvitationOut] = Field(default_factory=list)
