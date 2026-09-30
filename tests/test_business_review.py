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
        "phones": ["+373 22 221 303", "(022) 78-03-02"],
        "category": "veterinarian",
        "city": "Chișinău",
        "address": "Vasile Lupu 59, Chisinau",
        "timezone": "Europe/Chisinau",
        "opening_hours": HOURS,
        "latitude": 47.0217,
        "longitude": 28.8004,
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
            assert queue.json()[0]["phones"] == ["+373 22 221 303", "(022) 78-03-02"]

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
            json=_payload(phones=["  "], timezone="Not/AZone"),
            headers=HEADERS_A,
        )
        assert response.status_code in (400, 422)


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
