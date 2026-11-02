"""Owner / branch-owner / admin access to first-party listing analytics."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

from bson import ObjectId

from app.core.config import settings
from tests.conftest import HEADERS_A, HEADERS_B, USER_A_UID, USER_B_UID

ADMIN = patch.object(settings, "RAGLY_ADMIN_EMAILS", "uid_user_b@test.com")


def _seed(mock_db):
    async def run():
        business = await mock_db.businesses.insert_one(
            {
                "name": "Jo-Vet",
                "status": "published",
                "visible": True,
                "owner_uid": USER_A_UID,
                "timezone": "Europe/Chisinau",
                "created_at": datetime.now(timezone.utc),
            }
        )
        business_id = str(business.inserted_id)
        first = await mock_db.business_locations.insert_one(
            {
                "business_id": business_id,
                "address": "Str. București 45",
                "city": "Chișinău",
                "timezone": "Europe/Chisinau",
                "status": "published",
                "visible": True,
                "is_main": True,
            }
        )
        second = await mock_db.business_locations.insert_one(
            {
                "business_id": business_id,
                "address": "Str. Alba Iulia 12",
                "city": "Chișinău",
                "timezone": "Europe/Chisinau",
                "status": "published",
                "visible": True,
                "is_main": False,
            }
        )
        await mock_db.business_members.insert_one(
            {
                "business_id": business_id,
                "user_id": USER_A_UID,
                "email": "uid_user_a@test.com",
                "role": "owner",
            }
        )
        await mock_db.business_members.insert_one(
            {
                "business_id": business_id,
                "user_id": USER_B_UID,
                "email": "uid_user_b@test.com",
                "role": "branch_owner",
                "location_id": str(first.inserted_id),
            }
        )
        return business_id, str(first.inserted_id), str(second.inserted_id)

    return asyncio.run(run())


def _hit(client, business_id, location_id, action="view", headers=None, ua="pet-owner"):
    return client.post(
        f"/api/v1/businesses/{business_id}/analytics/events",
        json={"location_id": location_id, "action": action},
        headers={"User-Agent": ua, **(headers or {})},
    )


def test_owner_reads_each_branch_and_staff_views_are_ignored(client, mock_db):
    business_id, loc_a, loc_b = _seed(mock_db)

    ignored = _hit(client, business_id, loc_a, headers=HEADERS_A)
    assert ignored.status_code == 204
    counted = _hit(client, business_id, loc_a, ua="visitor-one")
    assert counted.status_code == 204
    _hit(client, business_id, loc_a, action="call", ua="visitor-one")
    _hit(client, business_id, loc_a, action="directions", ua="visitor-one")
    _hit(client, business_id, loc_b, ua="visitor-two")

    same_window = _hit(client, business_id, loc_a, ua="visitor-one")
    assert same_window.status_code == 204

    first = client.get(
        f"/api/v1/businesses/{business_id}/analytics",
        headers=HEADERS_A,
        params={"location_id": loc_a, "days": 30},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["location_id"] == loc_a
    assert body["totals"]["views"] == 1
    assert body["totals"]["unique_visitors"] == 1
    assert body["totals"]["calls"] == 1
    assert body["totals"]["directions"] == 1
    assert body["busy_hours"][0]["hour"] >= 0
    assert len(body["by_day"]) == 30
    assert len(body["by_hour"]) == 24

    other = client.get(
        f"/api/v1/businesses/{business_id}/analytics",
        headers=HEADERS_A,
        params={"location_id": loc_b},
    )
    assert other.status_code == 200
    assert other.json()["totals"]["views"] == 1
    assert other.json()["location_id"] == loc_b


def test_branch_owner_sees_only_managed_branches(client, mock_db):
    business_id, loc_a, loc_b = _seed(mock_db)
    _hit(client, business_id, loc_a, ua="visitor-one")
    _hit(client, business_id, loc_b, ua="visitor-two")

    allowed = client.get(
        f"/api/v1/businesses/{business_id}/analytics",
        headers=HEADERS_B,
        params={"location_id": loc_a},
    )
    assert allowed.status_code == 200
    assert allowed.json()["totals"]["views"] == 1

    denied = client.get(
        f"/api/v1/businesses/{business_id}/analytics",
        headers=HEADERS_B,
        params={"location_id": loc_b},
    )
    assert denied.status_code == 403

    mine = client.get("/api/v1/businesses/mine", headers=HEADERS_B).json()
    assert mine["role"] == "branch_owner"
    assert mine["businesses"][0]["location_ids"] == [loc_a]
    assert [item["id"] for item in mine["business"]["locations"]] == [loc_a]


def test_branch_owner_can_manage_more_than_one_branch(client, mock_db):
    business_id, loc_a, loc_b = _seed(mock_db)
    asyncio.run(
        mock_db.business_members.insert_one(
            {
                "business_id": business_id,
                "user_id": USER_B_UID,
                "email": "uid_user_b@test.com",
                "role": "branch_owner",
                "location_id": loc_b,
            }
        )
    )
    mine = client.get("/api/v1/businesses/mine", headers=HEADERS_B).json()
    assert set(mine["businesses"][0]["location_ids"]) == {loc_a, loc_b}
    assert {item["id"] for item in mine["business"]["locations"]} == {loc_a, loc_b}

    for location_id in (loc_a, loc_b):
        row = client.get(
            f"/api/v1/businesses/{business_id}/analytics",
            headers=HEADERS_B,
            params={"location_id": location_id},
        )
        assert row.status_code == 200, row.text


def test_pet_owner_cannot_read_analytics(client, mock_db):
    business_id, loc_a, _loc_b = _seed(mock_db)
    asyncio.run(mock_db.business_members.delete_many({"user_id": USER_B_UID}))
    denied = client.get(
        f"/api/v1/businesses/{business_id}/analytics",
        headers=HEADERS_B,
        params={"location_id": loc_a},
    )
    assert denied.status_code == 403
    missing = client.get(f"/api/v1/businesses/{business_id}/analytics")
    assert missing.status_code in {401, 403}


def test_admin_reads_any_branch(client, mock_db):
    business_id, loc_a, loc_b = _seed(mock_db)
    _hit(client, business_id, loc_b, ua="visitor-two")
    with ADMIN:
        directory = client.get("/api/v1/admin/analytics/directory", headers=HEADERS_B)
        assert directory.status_code == 200, directory.text
        assert directory.json()[0]["id"] == business_id
        assert len(directory.json()[0]["locations"]) == 2
        row = client.get(
            f"/api/v1/businesses/{business_id}/analytics",
            headers=HEADERS_B,
            params={"location_id": loc_b},
        )
        assert row.status_code == 200
        assert row.json()["totals"]["views"] == 1
        other = client.get(
            f"/api/v1/businesses/{business_id}/analytics",
            headers=HEADERS_B,
            params={"location_id": loc_a},
        )
        assert other.status_code == 200
