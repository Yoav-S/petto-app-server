"""Which country a coordinate falls in.

Borders are a simplified Natural Earth 10m map. A point on a coast or inside
one country is not treated as a neighbor just because that neighbor is close.
"""

import gzip
import json
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).resolve().parent / "data" / "countries.json.gz"
_Ring = tuple[float, float, float, float, list[list[float]]]
_COUNTRIES: list[tuple[str, list[_Ring]]] | None = None


def _load() -> list[tuple[str, list[_Ring]]]:
    global _COUNTRIES
    if _COUNTRIES is not None:
        return _COUNTRIES
    with gzip.open(_PATH, "rt", encoding="utf-8") as handle:
        raw = json.load(handle)
    countries: list[tuple[str, list[_Ring]]] = []
    for item in raw:
        rings: list[_Ring] = []
        for ring in item["r"]:
            xs = [point[0] for point in ring]
            ys = [point[1] for point in ring]
            rings.append((min(xs), max(xs), min(ys), max(ys), ring))
        countries.append((item["c"], rings))
    _COUNTRIES = countries
    return countries


def _inside(longitude: float, latitude: float, ring: list[list[float]]) -> bool:
    inside = False
    previous = len(ring) - 1
    for index, point in enumerate(ring):
        x_here, y_here = point
        x_prev, y_prev = ring[previous]
        if (y_here > latitude) != (y_prev > latitude):
            crossing = (x_prev - x_here) * (latitude - y_here) / (y_prev - y_here) + x_here
            if longitude < crossing:
                inside = not inside
        previous = index
    return inside


@lru_cache(maxsize=4096)
def _cached(lat_key: int, lng_key: int) -> str | None:
    latitude = lat_key / 10000
    longitude = lng_key / 10000
    best: tuple[str, float] | None = None
    for code, rings in _load():
        for min_lng, max_lng, min_lat, max_lat, ring in rings:
            if longitude < min_lng or longitude > max_lng or latitude < min_lat or latitude > max_lat:
                continue
            if not _inside(longitude, latitude, ring):
                continue
            area = (max_lng - min_lng) * (max_lat - min_lat)
            if best is None or area < best[1]:
                best = (code, area)
            break
    return best[0] if best else None


def country_code(latitude: float, longitude: float) -> str | None:
    """ISO country for this point. None when the point is not inside a country."""
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return _cached(round(latitude * 10000), round(longitude * 10000))
