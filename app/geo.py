"""Address search and the timezone of a map pin.

Search and reverse lookups go to Nominatim (OpenStreetMap). Nominatim's usage
policy asks for one request per second and an identifying user agent, so every
call goes through one lock with spacing, and answers are cached for a day.
Timezones come from timezonefinder, offline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from app.core.config import settings

logger = logging.getLogger("petto")

_MIN_INTERVAL = 1.05
_CACHE_TTL = 24 * 60 * 60
_CACHE_MAX = 4000
_LOCALES = {"en", "ro", "ru", "he"}


@dataclass(frozen=True)
class GeoPlace:
    label: str
    address: str
    city: str
    country_code: str | None
    latitude: float
    longitude: float
    place_ref: dict | None
    timezone: str | None


class GeoUnavailable(Exception):
    """The address provider did not answer."""


_lock = asyncio.Lock()
_last_call = 0.0
_cache: dict[str, tuple[float, list[GeoPlace]]] = {}
_finder = None


def timezone_at(latitude: float, longitude: float) -> str | None:
    """IANA timezone for a point, or None over the open sea."""
    global _finder
    try:
        if _finder is None:
            from timezonefinder import TimezoneFinder

            _finder = TimezoneFinder(in_memory=True)
        return _finder.timezone_at(lat=latitude, lng=longitude)
    except Exception:
        logger.exception("Timezone lookup failed for %s,%s", latitude, longitude)
        return None


def _user_agent() -> str:
    contact = settings.GEO_CONTACT_EMAIL.strip() or settings.SUPPORT_EMAIL.strip()
    return f"RaglyBusiness/1.0 ({contact})" if contact else "RaglyBusiness/1.0"


def _language(locale: str | None) -> str:
    code = (locale or "en").strip().lower().split("-")[0]
    return code if code in _LOCALES else "en"


def _cache_get(key: str) -> list[GeoPlace] | None:
    hit = _cache.get(key)
    if not hit:
        return None
    stored_at, value = hit
    if time.monotonic() - stored_at > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: list[GeoPlace]) -> None:
    if len(_cache) >= _CACHE_MAX:
        oldest = sorted(_cache.items(), key=lambda item: item[1][0])[: _CACHE_MAX // 10]
        for stale, _ in oldest:
            _cache.pop(stale, None)
    _cache[key] = (time.monotonic(), value)


def _city_of(address: dict) -> str:
    for key in ("city", "town", "village", "municipality", "hamlet", "suburb", "county", "state"):
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _street_line(address: dict, display_name: str) -> str:
    road = address.get("road") or address.get("pedestrian") or address.get("footway") or ""
    number = address.get("house_number") or ""
    if road:
        return f"{road} {number}".strip() if number else str(road)
    first = display_name.split(",")[0].strip()
    return first


def _place(item: dict) -> GeoPlace | None:
    try:
        latitude = float(item.get("lat"))
        longitude = float(item.get("lon"))
    except (TypeError, ValueError):
        return None
    address = item.get("address") if isinstance(item.get("address"), dict) else {}
    display = str(item.get("display_name") or "").strip()
    osm_type = str(item.get("osm_type") or "")[:1].upper()
    osm_id = item.get("osm_id")
    ref = {"provider": "osm", "id": f"{osm_type}{osm_id}"} if osm_type and osm_id is not None else None
    country = address.get("country_code")
    return GeoPlace(
        label=display,
        address=_street_line(address, display),
        city=_city_of(address),
        country_code=country.upper() if isinstance(country, str) and country else None,
        latitude=latitude,
        longitude=longitude,
        place_ref=ref,
        timezone=timezone_at(latitude, longitude),
    )


async def _request(path: str, params: dict, locale: str | None) -> list[dict]:
    global _last_call
    base = settings.NOMINATIM_URL.strip().rstrip("/")
    headers = {"User-Agent": _user_agent(), "Accept-Language": _language(locale)}
    async with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(f"{base}{path}", params=params, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("Nominatim request failed: %s", exc)
            raise GeoUnavailable() from exc
        finally:
            _last_call = time.monotonic()
    if response.status_code >= 400:
        logger.warning("Nominatim %s: %s", response.status_code, response.text[:200])
        raise GeoUnavailable()
    try:
        data = response.json()
    except ValueError as exc:
        raise GeoUnavailable() from exc
    if isinstance(data, dict):
        return [data] if data and "error" not in data else []
    return data if isinstance(data, list) else []


async def search(
    query: str,
    *,
    locale: str | None = None,
    near: tuple[float, float] | None = None,
    country: str | None = None,
    limit: int = 6,
) -> list[GeoPlace]:
    """Addresses matching `query`. `near` biases the ranking; `country` restricts it."""
    text = " ".join(query.split())[:200]
    if len(text) < 3:
        return []
    key = f"s|{_language(locale)}|{(country or '').lower()}|{text.lower()}|{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    params: dict = {
        "q": text,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": limit,
        "dedupe": 1,
    }
    if country:
        params["countrycodes"] = country.lower()
    if near is not None:
        latitude, longitude = near
        span = 0.5
        params["viewbox"] = f"{longitude - span},{latitude + span},{longitude + span},{latitude - span}"
        params["bounded"] = 0
    rows = await _request("/search", params, locale)
    places = [place for place in (_place(row) for row in rows) if place]
    _cache_put(key, places)
    return places


async def reverse(latitude: float, longitude: float, *, locale: str | None = None) -> GeoPlace | None:
    """The address under a dropped pin."""
    key = f"r|{_language(locale)}|{latitude:.5f}|{longitude:.5f}"
    cached = _cache_get(key)
    if cached is not None:
        return cached[0] if cached else None
    rows = await _request(
        "/reverse",
        {"lat": latitude, "lon": longitude, "format": "jsonv2", "addressdetails": 1, "zoom": 18},
        locale,
    )
    places = [place for place in (_place(row) for row in rows) if place]
    _cache_put(key, places)
    return places[0] if places else None
