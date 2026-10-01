"""Owner submit and Ragly-admin approve/reject."""

from unittest.mock import patch

from app.core.config import settings
from tests.conftest import HEADERS_A, HEADERS_B

HOURS = {
    "always_open": False,
    "mon": [{"open": "09:00", "close": "17:00"}],
    "tue": [{"open": "09:00", "close": "17:00"}],
    "wed": [{"open": "09:00", "close": "17:00"}],
    "thu": [{"open": "09:00", "close": "17:00"}],
    "fri": [{"open": "09:00", "close": "17:00"}],
    "sat": [{"open": "09:00", "close": "13:00"}],
    "sun": [],
}


def _payload(**overrides):
    body = {
        "name": "VetAsist Clinica Veterinară",
        "phone": ["+373 22 221 303", "(022) 78-03-02"],
        "category": "veterinarian",
        "city": "Chișinău",
        "address": "Vasile Lupu 59, Chisinau",
        "timezone": "Europe/Chisinau",
        "opening_hours": HOURS,
        "location": {"type": "Point", "coordinates": [28.8004, 47.0217]},
        "website": "https://vetasist.com.md/",
    }
    body.update(overrides)
    return body


def test_owner_submit_and_admin_approve(client):
    with patch("app.routers.businesses.send_business_review_email") as send:
        with patch.object(settings, "RAGLY_ADMIN_EMAILS", "uid_user_b@test.com"):
            created = client.post(
                "/api/v1/businesses", json=_payload(), headers=HEADERS_A
            )
            assert created.status_code == 201, created.text
            assert created.json()["status"] == "pending_review"
            assert send.call_count == 1

            forbidden = client.get("/api/v1/admin/businesses", headers=HEADERS_A)
            assert forbidden.status_code == 403

            queue = client.get("/api/v1/admin/businesses", headers=HEADERS_B)
            assert queue.status_code == 200
            business_id = queue.json()[0]["id"]
            assert queue.json()[0]["phone"] == ["+373 22 221 303", "(022) 78-03-02"]
            assert queue.json()[0]["location"]["coordinates"] == [28.8004, 47.0217]
            assert queue.json()[0]["owned"] is True

            approved = client.post(
                f"/api/v1/admin/businesses/{business_id}/approve",
                headers=HEADERS_B,
            )
            assert approved.status_code == 200
            assert approved.json()["status"] == "published"

            mine = client.get("/api/v1/businesses/mine", headers=HEADERS_A)
            assert mine.json()["is_ragly_admin"] is False
            assert mine.json()["business"]["status"] == "published"

            admin_session = client.get("/api/v1/businesses/mine", headers=HEADERS_B)
            assert admin_session.json()["is_ragly_admin"] is True


def test_incomplete_listing_is_rejected(client):
    with patch.object(settings, "RAGLY_ADMIN_EMAILS", ""):
        response = client.post(
            "/api/v1/businesses",
            json=_payload(phone=["  "], timezone="Not/AZone"),
            headers=HEADERS_A,
        )
        assert response.status_code in (400, 422)


def test_admin_can_publish_for_a_phone_request(client):
    with patch.object(settings, "RAGLY_ADMIN_EMAILS", "uid_user_b@test.com"):
        created = client.post(
            "/api/v1/admin/businesses",
            json=_payload(owner_email="clinic@example.com"),
            headers=HEADERS_B,
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "published"
        assert created.json()["owned"] is False
        assert created.json()["invitations"][0]["email"] == "clinic@example.com"
        assert created.json()["invitations"][0]["status"] == "pending"

        denied = client.post(
            "/api/v1/admin/businesses",
            json=_payload(owner_email="other@example.com"),
            headers=HEADERS_A,
        )
        assert denied.status_code == 403


def test_admin_reject_stores_reason(client):
    with patch("app.routers.businesses.send_business_review_email"):
        with patch.object(settings, "RAGLY_ADMIN_EMAILS", "uid_user_b@test.com"):
            created = client.post(
                "/api/v1/businesses", json=_payload(), headers=HEADERS_A
            )
            business_id = created.json()["id"]
            rejected = client.post(
                f"/api/v1/admin/businesses/{business_id}/reject",
                json={"reason": "Not a real clinic."},
                headers=HEADERS_B,
            )
            assert rejected.status_code == 200
            assert rejected.json()["status"] == "rejected"
            assert rejected.json()["rejection_reason"] == "Not a real clinic."


def test_manual_clinic_document_lists_and_updates(client, mock_db):
    import asyncio

    from bson import ObjectId

    doc_id = ObjectId()
    asyncio.run(
        mock_db.businesses.insert_one(
            {
                "_id": doc_id,
                "name": "Vetgor",
                "phone": "+373 606 97 607",
                "email": None,
                "description": "Cabinet veterinar",
                "category": "veterinarian",
                "city": "Chișinău",
                "status": "published",
                "address": "str. Columna 59, Chisinau",
                "timezone": "Europe/Chisinau",
                "opening_hours": {
                    "mon": [],
                    "tue": [{"open": "08:00", "close": "19:00"}],
                    "wed": [{"open": "08:00", "close": "19:00"}],
                    "thu": [{"open": "08:00", "close": "19:00"}],
                    "fri": [{"open": "08:00", "close": "19:00"}],
                    "sat": [{"open": "08:00", "close": "19:00"}],
                    "sun": [{"open": "08:00", "close": "19:00"}],
                },
                "website": None,
                "location": {"type": "Point", "coordinates": [28.845684, 47.0198926]},
                "photo": "vetgor.png",
            }
        )
    )
    with patch.object(settings, "RAGLY_ADMIN_EMAILS", "uid_user_b@test.com"):
        listed = client.get("/api/v1/admin/businesses", headers=HEADERS_B)
        assert listed.status_code == 200, listed.text
        row = listed.json()[0]
        assert row["name"] == "Vetgor"
        assert row["phone"] == ["+373 606 97 607"]
        assert row["photo"] == "vetgor.png"
        assert row["owned"] is False
        assert row["location"]["coordinates"][1] == 47.0198926

        updated = client.patch(
            f"/api/v1/admin/businesses/{doc_id}",
            json=_payload(name="Vetgor Updated", email="vetgor@example.com"),
            headers=HEADERS_B,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["name"] == "Vetgor Updated"
        assert updated.json()["email"] == "vetgor@example.com"
        assert updated.json()["owned"] is False
        assert updated.json()["status"] == "published"


def test_owner_invite_is_membership_only_after_approval(client):
    with patch.object(settings, "RAGLY_ADMIN_EMAILS", "uid_user_b@test.com"):
        created = client.post(
            "/api/v1/admin/businesses",
            json=_payload(owner_email="uid_user_a@test.com"),
            headers=HEADERS_B,
        )
        assert created.status_code == 201, created.text
        business_id = created.json()["id"]
        invitation_id = created.json()["invitations"][0]["id"]

        waiting = client.get("/api/v1/businesses/mine", headers=HEADERS_A)
        assert waiting.json()["business"] is None
        assert waiting.json()["invitations"][0]["status"] == "pending"

        denied = client.post(
            f"/api/v1/businesses/invitations/{invitation_id}/approve",
            headers=HEADERS_B,
        )
        assert denied.status_code == 403

        approved = client.post(
            f"/api/v1/businesses/invitations/{invitation_id}/approve",
            headers=HEADERS_A,
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        mine = client.get("/api/v1/businesses/mine", headers=HEADERS_A)
        assert mine.json()["role"] == "owner"
        assert mine.json()["business"]["owned"] is True

        queue = client.get("/api/v1/admin/businesses", headers=HEADERS_B)
        listed = next(item for item in queue.json() if item["id"] == business_id)
        assert listed["invitations"][0]["status"] == "approved"
        assert listed["owned"] is True

        worker = client.post(
            f"/api/v1/businesses/{business_id}/invitations",
            json={"email": "worker@example.com", "role": "worker"},
            headers=HEADERS_A,
        )
        assert worker.status_code == 201, worker.text
        assert worker.json()["status"] == "pending"

        removed = client.delete(f"/api/v1/businesses/{business_id}", headers=HEADERS_A)
        assert removed.status_code == 204
        gone = client.get("/api/v1/admin/businesses", headers=HEADERS_B)
        assert all(item["id"] != business_id for item in gone.json())


def test_worker_cannot_delete_business(client, mock_db):
    import asyncio

    with patch.object(settings, "RAGLY_ADMIN_EMAILS", "uid_user_b@test.com"):
        created = client.post(
            "/api/v1/admin/businesses",
            json=_payload(owner_email="uid_user_a@test.com"),
            headers=HEADERS_B,
        )
        business_id = created.json()["id"]
        invitation_id = created.json()["invitations"][0]["id"]
        client.post(
            f"/api/v1/businesses/invitations/{invitation_id}/approve",
            headers=HEADERS_A,
        )
        asyncio.run(
            mock_db.business_members.update_one(
                {"business_id": business_id, "user_id": "uid_user_a"},
                {"$set": {"role": "worker"}},
            )
        )
        denied = client.delete(f"/api/v1/businesses/{business_id}", headers=HEADERS_A)
        assert denied.status_code == 403
        removed = client.delete(
            f"/api/v1/admin/businesses/{business_id}",
            headers=HEADERS_B,
        )
        assert removed.status_code == 204
