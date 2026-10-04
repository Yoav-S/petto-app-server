"""A business is one brand. Each branch is its own address, hours, and phone."""
import asyncio
from datetime import datetime, timezone

from tests.conftest import HEADERS_A, USER_A_UID


def _business(name: str, phone: list[str], address: str, lng: float, lat: float) -> dict:
    return {
        "firebase_uid": USER_A_UID,
        "name": name,
        "phone": phone,
        "category": "veterinarian",
        "city": "Chișinău",
        "status": "published",
        "address": address,
        "timezone": "Europe/Chisinau",
        "opening_hours": {"always_open": False, "mon": [{"open": "09:00", "close": "19:00"}]},
        "location": {"type": "Point", "coordinates": [lng, lat]},
        "owner_uid": USER_A_UID,
        "created_at": datetime.now(timezone.utc),
    }


def test_nearby_returns_one_card_for_the_closest_branch(client, mock_db):
    async def seed():
        result = await mock_db.businesses.insert_one(
            _business("Zoomama", ["+373 22 000 000"], "Str. București 45", 28.86, 47.02)
        )
        business_id = str(result.inserted_id)
        await mock_db.business_locations.insert_one(
            {
                "business_id": business_id,
                "address": "Str. București 45",
                "city": "Chișinău",
                "timezone": "Europe/Chisinau",
                "opening_hours": {"always_open": False, "mon": [{"open": "09:00", "close": "19:00"}]},
                "location": {"type": "Point", "coordinates": [28.86, 47.02]},
                "phone": [],
            }
        )
        await mock_db.business_locations.insert_one(
            {
                "business_id": business_id,
                "address": "Str. Alba Iulia 12",
                "city": "Chișinău",
                "timezone": "Europe/Chisinau",
                "opening_hours": {"always_open": False, "mon": [{"open": "10:00", "close": "20:00"}]},
                "location": {"type": "Point", "coordinates": [28.80, 47.01]},
                "phone": ["+373 60 111 111"],
            }
        )

    asyncio.run(seed())
    nearby = client.get(
        "/api/v1/businesses/nearby",
        headers=HEADERS_A,
        params={"latitude": 47.01, "longitude": 28.80},
    )
    assert nearby.status_code == 200, nearby.text
    items = nearby.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Zoomama"
    assert items[0]["location_count"] == 2
    assert items[0]["address"] == "Str. Alba Iulia 12"

    detail = client.get(
        f"/api/v1/businesses/{items[0]['id']}",
        headers=HEADERS_A,
        params={"latitude": 47.01, "longitude": 28.80},
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["phone"] == ["+373 22 000 000"]
    branches = {item["address"]: item for item in body["locations"]}
    assert branches["Str. București 45"]["shared_phone"] is True
    assert branches["Str. București 45"]["phone"] == []
    assert branches["Str. București 45"]["call_phone"] == ["+373 22 000 000"]
    assert branches["Str. Alba Iulia 12"]["shared_phone"] is False
    assert branches["Str. Alba Iulia 12"]["phone"] == ["+373 60 111 111"]


def test_owner_sees_every_business_and_can_add_a_store(client, mock_db):
    async def seed():
        await mock_db.businesses.insert_one(
            _business("Zoomama", ["+373 22 000 000"], "Str. București 45", 28.86, 47.02)
        )
        second = _business("Pet Shop", ["+373 22 000 001"], "Str. Puskin 1", 28.83, 47.01)
        second["category"] = "pet_store"
        await mock_db.businesses.insert_one(second)

    asyncio.run(seed())
    session = client.get("/api/v1/businesses/mine", headers=HEADERS_A)
    assert session.status_code == 200, session.text
    names = sorted(item["business"]["name"] for item in session.json()["businesses"])
    assert names == ["Pet Shop", "Zoomama"]
    zoomama = next(item for item in session.json()["businesses"] if item["business"]["name"] == "Zoomama")
    assert zoomama["role"] == "owner"
    assert len(zoomama["business"]["locations"]) == 1

    added = client.post(
        f"/api/v1/businesses/{zoomama['business']['id']}/locations",
        headers=HEADERS_A,
        json={
            "address": "Str. Alba Iulia 12",
            "city": "Chișinău",
            "timezone": "Europe/Chisinau",
            "opening_hours": {"always_open": False, "mon": [{"open": "10:00", "close": "20:00"}]},
            "location": {"type": "Point", "coordinates": [28.80, 47.01]},
            "phone": ["+373 60 111 111"],
        },
    )
    assert added.status_code == 201, added.text
    assert added.json()["phone"] == ["+373 60 111 111"]
