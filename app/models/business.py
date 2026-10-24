"""Business, branch, and service documents as the API sends and receives them.

A business is the brand (name, logo, about, general contacts, owner).
A branch (`business_locations`) is one address with its own map point, contacts,
status, and visibility. A service is one category offered at a branch, with its
own opening hours, stored inside the branch document.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.contact import clean_email, clean_instagram, clean_phone_list, clean_website

BusinessCategory = Literal[
    "veterinarian",
    "groomer",
    "pharmacy",
    "pet_friendly",
    "pet_store",
]
CATEGORIES: tuple[str, ...] = ("veterinarian", "groomer", "pharmacy", "pet_friendly", "pet_store")

BusinessStatus = Literal[
    "draft",
    "pending_review",
    "published",
    "rejected",
    "suspended",
]
BranchStatus = Literal["draft", "pending_review", "published", "rejected"]
Locale = Literal["en", "ro", "ru", "he"]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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


class SpecialDate(BaseModel):
    """One calendar day that replaces the weekly hours: closed, or other hours."""

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    closed: bool = False
    slots: list[TimeSlot] = Field(default_factory=list)


class OpeningHours(BaseModel):
    """`provided` is false when there are no fixed hours to publish.

    Each day is a list of slots, so a break is two slots. A slot whose close is
    earlier than its open ends the next day (20:00–02:00).
    """

    provided: bool = True
    always_open: bool = False
    mon: list[TimeSlot] = Field(default_factory=list)
    tue: list[TimeSlot] = Field(default_factory=list)
    wed: list[TimeSlot] = Field(default_factory=list)
    thu: list[TimeSlot] = Field(default_factory=list)
    fri: list[TimeSlot] = Field(default_factory=list)
    sat: list[TimeSlot] = Field(default_factory=list)
    sun: list[TimeSlot] = Field(default_factory=list)
    special_dates: list[SpecialDate] = Field(default_factory=list)


def _owner_email(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    if "@" not in text or "." not in text.split("@")[-1]:
        raise ValueError("owner_email")
    return text


class BusinessDetails(BaseModel):
    """The business itself: what onboarding collects and what Edit business changes.

    Category, address, hours, and the map point live on branches, not here.
    The logo is uploaded separately and stored in `photo`.
    """

    name: str = Field(min_length=1, max_length=200)
    phone: list[str] = Field(default_factory=list)
    email: Optional[str] = None
    description: Optional[str] = Field(default=None, max_length=2000)
    website: Optional[str] = None
    instagram: Optional[str] = None
    locale: Optional[Locale] = None

    @field_validator("phone")
    @classmethod
    def phone_optional(cls, value: list[str]) -> list[str]:
        return clean_phone_list(value, required=False)

    @field_validator("email")
    @classmethod
    def email_optional(cls, value: Optional[str]) -> Optional[str]:
        return clean_email(value)

    @field_validator("website")
    @classmethod
    def website_optional(cls, value: Optional[str]) -> Optional[str]:
        return clean_website(value)

    @field_validator("instagram")
    @classmethod
    def instagram_optional(cls, value: Optional[str]) -> Optional[str]:
        return clean_instagram(value)


class ServiceWrite(BaseModel):
    """One category at a branch with its own hours. `id` keeps an existing service's identity."""

    id: Optional[str] = None
    category: BusinessCategory
    opening_hours: OpeningHours = Field(default_factory=OpeningHours)
    phone: list[str] = Field(default_factory=list)

    @field_validator("phone")
    @classmethod
    def phone_clean(cls, value: list[str]) -> list[str]:
        return clean_phone_list(value, required=False)


class PlaceRef(BaseModel):
    provider: Literal["osm", "google"]
    id: str = Field(min_length=1, max_length=200)


class BranchWrite(BaseModel):
    """One branch as the form sends it. Everything but the address may be empty on a draft."""

    address: str = Field(default="", max_length=300)
    city: str = Field(default="", max_length=120)
    location: Optional[GeoLocation] = None
    place_ref: Optional[PlaceRef] = None
    services: list[ServiceWrite] = Field(default_factory=list)
    phone: Optional[list[str]] = None
    website: Optional[str] = None
    instagram: Optional[str] = None
    photo: Optional[str] = None
    timezone: Optional[str] = Field(default=None, max_length=64)

    @field_validator("phone")
    @classmethod
    def phone_clean(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        return clean_phone_list(value, required=False)

    @field_validator("website")
    @classmethod
    def website_optional(cls, value: Optional[str]) -> Optional[str]:
        return clean_website(value)

    @field_validator("instagram")
    @classmethod
    def instagram_optional(cls, value: Optional[str]) -> Optional[str]:
        return clean_instagram(value)


# The old name, still imported by older code paths.
LocationWrite = BranchWrite


class ServiceOut(BaseModel):
    id: str
    category: BusinessCategory
    opening_hours: OpeningHours
    phone: list[str] = Field(default_factory=list)
    status: BranchStatus = "published"
    visible: bool = True


class LocationOut(BaseModel):
    id: str
    business_id: str
    is_main: bool = False
    address: str = ""
    city: str = ""
    timezone: str = "Europe/Chisinau"
    location: Optional[GeoLocation] = None
    place_ref: Optional[PlaceRef] = None
    services: list[ServiceOut] = Field(default_factory=list)
    # Branch-level hours: the first service's hours, kept for older clients.
    opening_hours: OpeningHours = Field(default_factory=OpeningHours)
    phone: list[str] = Field(default_factory=list)
    website: Optional[str] = None
    instagram: Optional[str] = None
    photo: Optional[str] = None
    status: BranchStatus = "published"
    visible: bool = True
    hidden_reason: Optional[str] = None
    hidden_by: Optional[Literal["owner", "admin"]] = None
    rejection_reason: Optional[str] = None
    field_errors: dict[str, str] = Field(default_factory=dict)
    admin_notes: dict[str, str] = Field(default_factory=dict)
    submitted_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


_BUSINESS_REVIEW_FIELDS = frozenset(
    {
        "name",
        "phone",
        "email",
        "description",
        "website",
        "photo",
        "instagram",
        "general",
    }
)
_BRANCH_REVIEW_FIELDS = frozenset(
    {
        "address",
        "city",
        "location",
        "services",
        "hours",
        "phone",
        "website",
        "instagram",
        "photo",
        "general",
    }
)


def _clean_notes(value: dict[str, str], allowed: frozenset[str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, message in value.items():
        if not isinstance(key, str) or not isinstance(message, str):
            continue
        base = key.split(":", 1)[0]
        if base not in allowed and base != "service":
            continue
        text = message.strip()
        if text:
            cleaned[key[:80]] = text[:500]
    return cleaned


class BusinessReject(BaseModel):
    field_errors: dict[str, str]

    @field_validator("field_errors")
    @classmethod
    def notes_present(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned = _clean_notes(value, _BUSINESS_REVIEW_FIELDS | _BRANCH_REVIEW_FIELDS)
        if not cleaned:
            raise ValueError("field_errors")
        return cleaned


class AdminApprove(BaseModel):
    """Optional notes the owner still sees after the request is accepted."""

    field_notes: dict[str, str] = Field(default_factory=dict)

    @field_validator("field_notes")
    @classmethod
    def notes_clean(cls, value: dict[str, str]) -> dict[str, str]:
        return _clean_notes(value, _BUSINESS_REVIEW_FIELDS | _BRANCH_REVIEW_FIELDS)


class BranchReject(BaseModel):
    """Send a branch back. Notes per field; `reason` alone becomes a general note."""

    field_errors: dict[str, str] = Field(default_factory=dict)
    reason: Optional[str] = Field(default=None, max_length=500)

    def notes(self) -> dict[str, str]:
        cleaned = _clean_notes(self.field_errors, _BRANCH_REVIEW_FIELDS)
        text = (self.reason or "").strip()
        if text and "general" not in cleaned:
            cleaned["general"] = text[:500]
        return cleaned


class BranchVisibility(BaseModel):
    """Pause or resume a live branch in the app."""

    visible: bool
    reason: Optional[str] = Field(default=None, max_length=300)


class ListingVisibility(BaseModel):
    """Hide or show the whole business in the app. Takes effect immediately."""

    visible: bool


class BranchApprove(BaseModel):
    field_notes: dict[str, str] = Field(default_factory=dict)

    @field_validator("field_notes")
    @classmethod
    def notes_clean(cls, value: dict[str, str]) -> dict[str, str]:
        return _clean_notes(value, _BRANCH_REVIEW_FIELDS)


class AdminCreate(BaseModel):
    """The admin one-form: the business, its first branch, and an optional owner."""

    business: BusinessDetails
    branch: BranchWrite
    owner_email: Optional[str] = None

    @field_validator("owner_email")
    @classmethod
    def owner_email_optional(cls, value: Optional[str]) -> Optional[str]:
        return _owner_email(value)


MemberRole = Literal["owner", "branch_owner", "lead", "worker"]


class StoreReject(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_present(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("reason")
        return text


class PlaceService(BaseModel):
    """One service of a branch on the public business screen."""

    id: str
    category: BusinessCategory
    opening_hours: OpeningHours
    phone: list[str] = Field(default_factory=list)


class PlaceLocation(BaseModel):
    """A branch on the public business screen.

    `phone` is only the branch number. `call_phone` is the number to dial:
    the branch number, or the business number when the branch has none.
    `opening_hours` is the first service's hours, for app versions that show
    one schedule per branch. `services` carries every service.
    """

    id: str
    address: str
    city: str
    opening_hours: OpeningHours
    phone: list[str] = Field(default_factory=list)
    call_phone: list[str] = Field(default_factory=list)
    shared_phone: bool = False
    location: Optional[GeoLocation] = None
    distance_km: Optional[float] = None
    categories: list[BusinessCategory] = Field(default_factory=list)
    services: list[PlaceService] = Field(default_factory=list)
    photo: Optional[str] = None


class InvitationOut(BaseModel):
    id: str
    business_id: str
    business_name: str = ""
    email: str
    role: MemberRole
    status: Literal["pending", "approved", "declined"]
    location_id: Optional[str] = None


class BusinessOut(BaseModel):
    id: str
    name: str
    phone: list[str]
    email: Optional[str] = None
    description: Optional[str] = None
    # Mirrored from the main branch's first service, for older clients and filters.
    category: Optional[BusinessCategory] = None
    categories: list[BusinessCategory] = Field(default_factory=list)
    city: str = ""
    status: BusinessStatus
    """False hides every branch of this business from the app. Not a review status."""
    visible: bool = True
    address: str = ""
    timezone: str = "Europe/Chisinau"
    opening_hours: OpeningHours = Field(default_factory=OpeningHours)
    website: Optional[str] = None
    location: Optional[GeoLocation] = None
    photo: Optional[str] = None
    photos: list[str] = Field(default_factory=list)
    owner_uid: Optional[str] = None
    owner_email: Optional[str] = None
    owned: bool = False
    instagram: Optional[str] = None
    locale: Locale = "en"
    rejection_reason: Optional[str] = None
    field_errors: dict[str, str] = Field(default_factory=dict)
    admin_notes: dict[str, str] = Field(default_factory=dict)
    reviewed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    invitations: list[InvitationOut] = Field(default_factory=list)
    locations: list[LocationOut] = Field(default_factory=list)


class PhotoRemove(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


class OwnerInvite(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class TeamInvite(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: MemberRole
    location_id: Optional[str] = None
    reports_to: Optional[str] = None


class BusinessCounts(BaseModel):
    pending: int = 0
    published: int = 0
    rejected: int = 0
    deleted: int = 0


class BusinessPage(BaseModel):
    items: list[BusinessOut]
    has_more: bool = False


class NameCheck(BaseModel):
    available: bool
    name_key: str
    """True when the name is taken and that listing has no owner yet. The
    website then offers Contact us instead of a plain 'name taken' error."""
    claimable: bool = False


class EventOut(BaseModel):
    id: str
    business_id: str
    business_name: str = ""
    location_id: Optional[str] = None
    address: Optional[str] = None
    kind: str
    actor_email: Optional[str] = None
    audience: Literal["owner", "admins"]
    message: Optional[str] = None
    created_at: datetime


class PlaceConflict(BaseModel):
    """Another business already at this place, shown to the admin."""

    business_id: str
    business_name: str
    address: str
    distance_m: int
    same_place_ref: bool = False


class ReviewBranch(BaseModel):
    branch: LocationOut
    conflicts: list[PlaceConflict] = Field(default_factory=list)


class ReviewGroup(BaseModel):
    """One business in the admin queue with every branch of it that waits."""

    business: BusinessOut
    branches: list[ReviewBranch]
    first_request: bool
    owner_email: Optional[str] = None
    owner_since: Optional[datetime] = None
    waiting_since: Optional[datetime] = None


class BusinessPlace(BaseModel):
    """One live branch on Near you. `id` is the business, `location_id` is the branch."""

    id: str
    name: str
    category: BusinessCategory
    categories: list[BusinessCategory] = Field(default_factory=list)
    city: str
    image: Optional[str] = None
    distance_km: Optional[float] = None
    rating: Optional[float] = None
    open_now: Optional[bool] = None
    open_24_7: bool = False
    closes_at: Optional[str] = None
    opens_at: Optional[str] = None
    next_open_day: Optional[Weekday] = None
    opens_tomorrow: bool = False
    address: str = ""
    location_count: int = 1
    location_id: Optional[str] = None


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


class ReviewReportWrite(BaseModel):
    """Why this review was reported. The reporter cannot be the author."""

    reason: Literal["spam", "offensive", "fake", "irrelevant"]


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
    locations: list[PlaceLocation] = Field(default_factory=list)


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


class BusinessMembership(BaseModel):
    """One business this account can open. An owner has no location_id: every branch."""

    business: BusinessOut
    role: MemberRole
    location_id: Optional[str] = None


class BusinessSession(BaseModel):
    is_ragly_admin: bool
    business: Optional[BusinessOut] = None
    businesses: list[BusinessMembership] = Field(default_factory=list)
    role: Optional[MemberRole] = None
    invitations: list[InvitationOut] = Field(default_factory=list)
    notices: list[EventOut] = Field(default_factory=list)
    locale: Locale = "en"
