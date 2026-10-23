"""Branches of existing businesses, and the startup migration of older documents."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

from app.core.config import settings
from app.core.database import _migrate_business_branches
from tests.conftest import HEADERS_A, HEADERS_B, USER_A_UID

ADMIN = patch.object(settings, "RAGLY_ADMIN_EMAILS", "uid_user_b@test.com")


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


def _old_branch(business_id: str, address: str, lng: float, lat: float, phone: list[str], **extra) -> dict:
    return {
        "business_id": business_id,
        "address": address,
        "city": "Chișinău",
        "timezone": "Europe/Chisinau",
        "opening_hours": {"always_open": False, "mon": [{"open": "10:00", "close": "20:00"}]},
        "location": {"type": "Point", "coordinates": [lng, lat]},
        "phone": phone,
        **extra,
    }


def test_nearby_returns_one_card_for_the_closest_branch(client, mock_db):
    async def seed():
        result = await mock_db.businesses.insert_one(_business("Zoomama", ["+373 22 000 000"], "Str. București 45", 28.86, 47.02))
        business_id = str(result.inserted_id)
        await mock_db.business_locations.insert_one(_old_branch(business_id, "Str. București 45", 28.86, 47.02, []))
        await mock_db.business_locations.insert_one(_old_branch(business_id, "Str. Alba Iulia 12", 28.80, 47.01, ["+373 60 111 111"]))
        await mock_db.business_locations.insert_one(_old_branch(business_id, "Str. Pending 1", 28.79, 47.00, [], status="pending_review"))

    asyncio.run(seed())
    nearby = client.get("/api/v1/businesses/nearby", headers=HEADERS_A, params={"latitude": 47.01, "longitude": 28.80})
    assert nearby.status_code == 200, nearby.text
    items = nearby.json()["items"]
    assert [item["address"] for item in items] == ["Str. Alba Iulia 12", "Str. București 45"]
    assert {item["name"] for item in items} == {"Zoomama"}
    assert items[0]["location_count"] == 1
    assert items[0]["category"] == "veterinarian"
    assert items[0]["location_id"]

    detail = client.get(f"/api/v1/businesses/{items[0]['id']}", headers=HEADERS_A, params={"latitude": 47.01, "longitude": 28.80})
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["phone"] == ["+373 22 000 000"]
    branches = {item["address"]: item for item in body["locations"]}
    assert branches["Str. București 45"]["shared_phone"] is True
    assert branches["Str. București 45"]["call_phone"] == ["+373 22 000 000"]
    assert branches["Str. Alba Iulia 12"]["shared_phone"] is False
    assert branches["Str. Alba Iulia 12"]["phone"] == ["+373 60 111 111"]
    assert branches["Str. Alba Iulia 12"]["services"][0]["category"] == "veterinarian"
    assert "Str. Pending 1" not in branches


def test_owner_sees_every_business_and_can_add_a_branch(client, mock_db):
    async def seed():
        await mock_db.businesses.insert_one(_business("Zoomama", ["+373 22 000 000"], "Str. București 45", 28.86, 47.02))
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
    assert zoomama["business"]["locations"][0]["services"][0]["category"] == "veterinarian"
    business_id = zoomama["business"]["id"]

    with patch("app.routers.businesses.send_business_review_email"), patch("app.business_mail.send_business_owner_email"), ADMIN:
        added = client.post(
            f"/api/v1/businesses/{business_id}/locations",
            headers=HEADERS_A,
            json={
                "address": "Str. Alba Iulia 12",
                "city": "Chișinău",
                "location": {"type": "Point", "coordinates": [28.80, 47.01]},
                "services": [{"category": "groomer", "opening_hours": {"always_open": False, "mon": [{"open": "10:00", "close": "20:00"}]}}],
                "phone": ["+373 60 111 111"],
            },
        )
        assert added.status_code == 201, added.text
        assert added.json()["phone"] == ["+373 60 111 111"]
        assert added.json()["status"] == "draft"
        assert added.json()["is_main"] is False
        branch_id = added.json()["id"]
        sent = client.post(f"/api/v1/businesses/{business_id}/locations/{branch_id}/send", headers=HEADERS_A)
        assert sent.status_code == 200, sent.text
        assert sent.json()["status"] == "pending_review"

        still_live = client.get("/api/v1/businesses/mine", headers=HEADERS_A).json()
        zoomama = next(item for item in still_live["businesses"] if item["business"]["name"] == "Zoomama")
        assert zoomama["business"]["status"] == "published"
        assert zoomama["business"]["categories"] == ["veterinarian", "groomer"]
        assert zoomama["business"]["address"] == "Str. București 45"

        hidden = client.get("/api/v1/businesses/nearby", headers=HEADERS_A, params={"latitude": 47.01, "longitude": 28.80})
        cards = [item for item in hidden.json()["items"] if item["name"] == "Zoomama"]
        assert [item["address"] for item in cards] == ["Str. București 45"]

        queue = client.get("/api/v1/admin/locations", headers=HEADERS_B)
        assert queue.status_code == 200, queue.text
        assert len(queue.json()) == 1
        assert queue.json()[0]["address"] == "Str. Alba Iulia 12"
        grouped = client.get("/api/v1/admin/review", headers=HEADERS_B).json()
        assert grouped[0]["first_request"] is False
        assert grouped[0]["business"]["name"] == "Zoomama"
        blocked = client.post(f"/api/v1/admin/locations/{branch_id}/approve", headers=HEADERS_A)
        assert blocked.status_code == 403
        approved = client.post(f"/api/v1/admin/locations/{branch_id}/approve", headers=HEADERS_B)
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "published"

    visible = client.get("/api/v1/businesses/nearby", headers=HEADERS_A, params={"latitude": 47.01, "longitude": 28.80})
    shown = [item for item in visible.json()["items"] if item["name"] == "Zoomama"]
    assert [item["address"] for item in shown] == ["Str. Alba Iulia 12", "Str. București 45"]
    assert shown[0]["categories"] == ["groomer"]
    assert shown[1]["categories"] == ["veterinarian"]
    groomers = client.get("/api/v1/businesses/nearby", headers=HEADERS_A, params={"latitude": 47.01, "longitude": 28.80, "category": "groomer"})
    assert [item["address"] for item in groomers.json()["items"]] == ["Str. Alba Iulia 12"]
    assert groomers.json()["items"][0]["categories"] == ["groomer"]


def test_nearby_keeps_other_cities_in_the_same_country_only(client, mock_db):
    """Chisinau can see Balti. It cannot see Amman, however close the border is."""
    async def seed():
        await mock_db.businesses.insert_one(_business("Balti Vet", ["+373 231 000"], "Str. Stefan 1", 27.91, 47.75))
        await mock_db.businesses.insert_one(_business("Amman Vet", ["+962 6 000 0000"], "Rainbow St", 35.93, 31.95))

    asyncio.run(seed())
    nearby = client.get("/api/v1/businesses/nearby", headers=HEADERS_A, params={"latitude": 47.01, "longitude": 28.86, "limit": 50})
    assert [item["name"] for item in nearby.json()["items"]] == ["Balti Vet"]
    assert nearby.json()["items"][0]["distance_km"] > 0
    israel = client.get("/api/v1/businesses/nearby", headers=HEADERS_A, params={"latitude": 32.08, "longitude": 34.78, "limit": 50})
    assert israel.json()["items"] == []


def test_startup_migration_fills_services_and_mirrors_once(client, mock_db):
    async def seed():
        result = await mock_db.businesses.insert_one(_business("Zoomama", ["+373 22 000 000"], "Str. București 45", 28.86, 47.02))
        business_id = str(result.inserted_id)
        await mock_db.business_locations.insert_one(_old_branch(business_id, "Str. București 45", 28.86, 47.02, []))
        await mock_db.business_locations.insert_one(_old_branch(business_id, "Str. Alba Iulia 12", 28.80, 47.01, ["+373 60 111 111"], status="rejected", rejection_reason="Wrong city"))
        legacy_only = _business("Vetgor", ["+373 606 97 607"], "str. Columna 59", 28.845, 47.019)
        legacy_only["phone"] = "+373 606 97 607"
        del legacy_only["owner_uid"]
        await mock_db.businesses.insert_one(legacy_only)
        return business_id

    business_id = asyncio.run(seed())
    asyncio.run(_migrate_business_branches(mock_db))

    rows = asyncio.run(mock_db.business_locations.find({"business_id": business_id}).sort("_id", 1).to_list(None))
    assert [row["is_main"] for row in rows] == [True, False]
    assert rows[0]["services"][0]["category"] == "veterinarian"
    assert rows[0]["services"][0]["status"] == "published"
    assert rows[0]["services"][0]["opening_hours"]["mon"] == [{"open": "10:00", "close": "20:00"}]
    assert rows[0]["visible"] is True
    assert rows[0]["source"] == "migrated"
    assert rows[1]["services"][0]["status"] == "rejected"
    assert rows[1]["rejection_reason"] == "Wrong city"
    assert set(rows[0]) == set(rows[1])

    business = asyncio.run(mock_db.businesses.find_one({"name": "Zoomama"}))
    assert business["categories"] == ["veterinarian"]
    assert business["locale"] == "en"
    assert business["photos"] == []
    assert business["opening_hours"]["mon"] == [{"open": "10:00", "close": "20:00"}]

    vetgor = asyncio.run(mock_db.businesses.find_one({"name": "Vetgor"}))
    vetgor_rows = asyncio.run(mock_db.business_locations.find({"business_id": str(vetgor["_id"])}).to_list(None))
    assert len(vetgor_rows) == 1
    assert vetgor_rows[0]["is_main"] is True
    assert vetgor_rows[0]["services"][0]["opening_hours"]["mon"] == [{"open": "09:00", "close": "19:00"}]
    assert vetgor["source"] == "admin"
    assert set(vetgor_rows[0]) == set(rows[0])

    snapshot = asyncio.run(mock_db.business_locations.find({}).sort("_id", 1).to_list(None))
    asyncio.run(_migrate_business_branches(mock_db))
    again = asyncio.run(mock_db.business_locations.find({}).sort("_id", 1).to_list(None))
    assert again == snapshot

    nearby = client.get("/api/v1/businesses/nearby", headers=HEADERS_A, params={"latitude": 47.02, "longitude": 28.86})
    assert [item["name"] for item in nearby.json()["items"]] == ["Zoomama", "Vetgor"]
    assert nearby.json()["items"][0]["location_count"] == 1
    detail = client.get(f"/api/v1/businesses/{vetgor['_id']}", headers=HEADERS_A).json()
    assert detail["phone"] == ["+373 606 97 607"]
    assert detail["locations"][0]["address"] == "str. Columna 59"
