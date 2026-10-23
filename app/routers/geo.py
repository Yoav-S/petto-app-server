"""Address search for the branch form. Signed-in business accounts only."""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app import geo
from app.core.errors import ErrorCode, raise_api_error
from app.middleware.auth import get_current_user

router = APIRouter(tags=["geo"])


class GeoPlaceOut(BaseModel):
    label: str
    address: str
    city: str
    country_code: Optional[str] = None
    latitude: float
    longitude: float
    place_ref: Optional[dict] = None
    timezone: Optional[str] = None


class GeoSearchOut(BaseModel):
    items: list[GeoPlaceOut] = Field(default_factory=list)


class GeoPointOut(BaseModel):
    place: Optional[GeoPlaceOut] = None
    timezone: Optional[str] = None


def _out(place: geo.GeoPlace) -> GeoPlaceOut:
    return GeoPlaceOut(
        label=place.label,
        address=place.address,
        city=place.city,
        country_code=place.country_code,
        latitude=place.latitude,
        longitude=place.longitude,
        place_ref=place.place_ref,
        timezone=place.timezone,
    )


@router.get("/geo/search", response_model=GeoSearchOut)
async def search_addresses(
    q: str = Query(min_length=1, max_length=200),
    latitude: float | None = Query(default=None, ge=-90, le=90),
    longitude: float | None = Query(default=None, ge=-180, le=180),
    country: str | None = Query(default=None, min_length=2, max_length=2),
    locale: str | None = Query(default=None, max_length=10),
    current_user: dict = Depends(get_current_user),
):
    """Addresses that match what the owner typed, closest to their map first."""
    del current_user
    near = (latitude, longitude) if latitude is not None and longitude is not None else None
    try:
        places = await geo.search(q, locale=locale, near=near, country=country)
    except geo.GeoUnavailable:
        raise_api_error(503, ErrorCode.GEO_UNAVAILABLE)
    return GeoSearchOut(items=[_out(place) for place in places])


@router.get("/geo/point", response_model=GeoPointOut)
async def describe_point(
    latitude: float = Query(ge=-90, le=90),
    longitude: float = Query(ge=-180, le=180),
    locale: str | None = Query(default=None, max_length=10),
    current_user: dict = Depends(get_current_user),
):
    """The address and timezone under a pin the owner dragged."""
    del current_user
    zone = geo.timezone_at(latitude, longitude)
    try:
        place = await geo.reverse(latitude, longitude, locale=locale)
    except geo.GeoUnavailable:
        return GeoPointOut(place=None, timezone=zone)
    return GeoPointOut(place=_out(place) if place else None, timezone=zone)
