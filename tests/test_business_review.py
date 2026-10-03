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

        unowned = client.post(
            "/api/v1/admin/businesses",
            json=_payload(name="No Owner Clinic"),
            headers=HEADERS_B,
        )
        assert unowned.status_code == 201, unowned.text
        assert unowned.json()["status"] == "published"
        assert unowned.json()["owned"] is False
        assert unowned.json()["invitations"] == []


def test_admin_reject_stores_reason(client):
    with patch("app.routers.businesses.send_business_review_email"):
        with patch.object(settings, "RAGLY_ADMIN_EMAILS", "uid_user_b@test.com"):
            created = client.post(
                "/api/v1/businesses", json=_payload(), headers=HEADERS_A
            )
            business_id = created.json()["id"]
            rejected = client.post(
                f"/api/v1/admin/businesses/{business_id}/reject",
                json={"field_errors": {"address": "This address is not in Chișinău."}},
                headers=HEADERS_B,
            )
            assert rejected.status_code == 200
            assert rejected.json()["status"] == "rejected"
            assert rejected.json()["field_errors"]["address"] == "This address is not in Chișinău."


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


def test_admin_and_owner_store_listing_photos(client):
    image = ("clinic.png", b"\x89PNG\r\n\x1a\nfake", "image/png")
    with patch.object(settings, "RAGLY_ADMIN_EMAILS", "uid_user_b@test.com"):
        with patch(
            "app.routers.businesses.upload_business_image",
            side_effect=[
                "https://firebasestorage.googleapis.com/v0/b/bucket/o/businesses%2F1%2Fa.png?alt=media&token=abc",
                "https://firebasestorage.googleapis.com/v0/b/bucket/o/businesses%2F1%2Fb.png?alt=media&token=def",
            ],
        ) as upload:
            with patch("app.routers.businesses.delete_business_image"):
                created = client.post(
                    "/api/v1/admin/businesses",
                    json=_payload(owner_email="uid_user_a@test.com", photo="vetgor.png"),
                    headers=HEADERS_B,
                )
                business_id = created.json()["id"]
                added = client.post(
                    f"/api/v1/admin/businesses/{business_id}/photos",
                    files={"file": image},
                    headers=HEADERS_B,
                )
                assert added.status_code == 200, added.text
                assert added.json()["photo"] == "vetgor.png"
                assert added.json()["photos"] == [
                    "https://firebasestorage.googleapis.com/v0/b/bucket/o/businesses%2F1%2Fa.png?alt=media&token=abc"
                ]
                assert upload.call_args.args[2] == "image/png"

                denied = client.post(
                    f"/api/v1/businesses/{business_id}/photos",
                    files={"file": image},
                    headers=HEADERS_A,
                )
                assert denied.status_code == 403

                invitation_id = created.json()["invitations"][0]["id"]
                client.post(
                    f"/api/v1/businesses/invitations/{invitation_id}/approve",
                    headers=HEADERS_A,
                )
                owned = client.post(
                    f"/api/v1/businesses/{business_id}/photos",
                    files={"file": image},
                    headers=HEADERS_A,
                )
                assert owned.status_code == 200, owned.text
                assert len(owned.json()["photos"]) == 2

                removed = client.request(
                    "DELETE",
                    f"/api/v1/admin/businesses/{business_id}/photos",
                    json={"url": owned.json()["photos"][0]},
                    headers=HEADERS_B,
                )
                assert removed.status_code == 200, removed.text
                assert len(removed.json()["photos"]) == 1
                assert removed.json()["photo"] == "vetgor.png"


def test_review_flow_emails_owner_and_counts(client, mock_db):
    """Submit, notify admins, reject with field notes, then approve the resubmit."""
    import asyncio

    with patch("app.routers.businesses.send_business_review_email") as admins:
        with patch("app.routers.businesses.send_business_owner_email") as owner_mail:
            with patch.object(
                settings, "RAGLY_ADMIN_EMAILS", "uid_user_b@test.com,second@example.com"
            ):
                created = client.post("/api/v1/businesses", json=_payload(), headers=HEADERS_A)
                assert created.status_code == 201, created.text
                business_id = created.json()["id"]
                assert created.json()["status"] == "pending_review"
                assert {call.args[0] for call in admins.call_args_list} == {
                    "uid_user_b@test.com",
                    "second@example.com",
                }
                assert owner_mail.call_args.kwargs["subject"].endswith("is waiting for review")

                waiting = client.get("/api/v1/businesses/mine", headers=HEADERS_A)
                assert waiting.json()["business"]["status"] == "pending_review"
                assert waiting.json()["role"] is None
                summary = client.get("/api/v1/admin/businesses/summary", headers=HEADERS_B)
                assert summary.json()["pending"] == 1
                assert summary.json()["published"] == 0
                assert summary.json()["rejected"] == 0
                members = asyncio.run(
                    mock_db.business_members.find({"business_id": business_id}).to_list(None)
                )
                assert members == []

                rejected = client.post(
                    f"/api/v1/admin/businesses/{business_id}/reject",
                    json={
                        "field_errors": {
                            "address": "This address is not in Chișinău.",
                            "phone": "Add the city code.",
                        }
                    },
                    headers=HEADERS_B,
                )
                assert rejected.status_code == 200, rejected.text
                notes = [
                    call.kwargs["lines"]
                    for call in owner_mail.call_args_list
                    if call.kwargs.get("lines")
                ]
                assert notes[-1] == [
                    "address: This address is not in Chișinău.",
                    "phone: Add the city code.",
                ]
                after_reject = client.get("/api/v1/admin/businesses/summary", headers=HEADERS_B)
                assert after_reject.json()["pending"] == 0
                assert after_reject.json()["rejected"] == 1

                resubmitted = client.post("/api/v1/businesses", json=_payload(), headers=HEADERS_A)
                assert resubmitted.status_code == 201, resubmitted.text
                assert resubmitted.json()["status"] == "pending_review"
                assert resubmitted.json()["field_errors"] == {}

                approved = client.post(
                    f"/api/v1/admin/businesses/{business_id}/approve",
                    headers=HEADERS_B,
                )
                assert approved.status_code == 200, approved.text
                assert approved.json()["status"] == "published"
                published_mail = owner_mail.call_args.kwargs
                assert "is published" in published_mail["subject"]
                member = asyncio.run(
                    mock_db.business_members.find_one(
                        {"business_id": business_id, "user_id": "uid_user_a"}
                    )
                )
                assert member["role"] == "owner"
                mine = client.get("/api/v1/businesses/mine", headers=HEADERS_A)
                assert mine.json()["role"] == "owner"
                finished = client.get("/api/v1/admin/businesses/summary", headers=HEADERS_B)
                assert finished.json()["pending"] == 0
                assert finished.json()["published"] == 1
                assert finished.json()["rejected"] == 0


def test_summary_counts_and_pages(client):
    with patch.object(settings, "RAGLY_ADMIN_EMAILS", "uid_user_b@test.com"):
        created = client.post(
            "/api/v1/admin/businesses",
            json=_payload(owner_email="uid_user_a@test.com"),
            headers=HEADERS_B,
        )
        business_id = created.json()["id"]
        summary = client.get("/api/v1/admin/businesses/summary", headers=HEADERS_B)
        assert summary.status_code == 200, summary.text
        assert summary.json()["published"] == 1
        assert summary.json()["deleted"] == 0

        page = client.get(
            "/api/v1/admin/businesses/page?limit=1&status=published",
            headers=HEADERS_B,
        )
        assert page.status_code == 200, page.text
        assert len(page.json()["items"]) == 1
        assert page.json()["items"][0]["id"] == business_id
        assert page.json()["has_more"] is False

        removed = client.delete(
            f"/api/v1/admin/businesses/{business_id}",
            headers=HEADERS_B,
        )
        assert removed.status_code == 204
        after = client.get("/api/v1/admin/businesses/summary", headers=HEADERS_B)
        assert after.json()["published"] == 0
        assert after.json()["deleted"] == 1


def test_nearby_lists_published_businesses_closest_first(client, mock_db):
    with patch.object(settings, "RAGLY_ADMIN_EMAILS", "uid_user_b@test.com"):
        near = client.post(
            "/api/v1/admin/businesses",
            json=_payload(name="Near Clinic", location={"type": "Point", "coordinates": [28.83, 47.02]}),
            headers=HEADERS_B,
        )
        far = client.post(
            "/api/v1/admin/businesses",
            json=_payload(name="Far Clinic", location={"type": "Point", "coordinates": [28.95, 47.20]}),
            headers=HEADERS_B,
        )
        assert near.status_code == 201, near.text
        assert far.status_code == 201, far.text
        import asyncio

        asyncio.run(
            mock_db.business_reviews.insert_one(
                {"business_id": near.json()["id"], "rating": 5}
            )
        )
        asyncio.run(
            mock_db.business_reviews.insert_one(
                {"business_id": near.json()["id"], "rating": 4}
            )
        )

        listed = client.get(
            "/api/v1/businesses/nearby?latitude=47.02&longitude=28.83&limit=1",
            headers=HEADERS_A,
        )
        assert listed.status_code == 200, listed.text
        page = listed.json()
        assert page["has_more"] is True
        assert page["items"][0]["name"] == "Near Clinic"
        assert page["items"][0]["distance_km"] == 0.0
        assert page["items"][0]["rating"] == 4.5

        anywhere = client.get(
            "/api/v1/businesses/nearby?anywhere=true&offset=0&limit=15",
            headers=HEADERS_A,
        )
        assert anywhere.status_code == 200, anywhere.text
        names = [item["name"] for item in anywhere.json()["items"]]
        assert names == ["Far Clinic", "Near Clinic"]


def test_nearby_treats_24_00_as_end_of_day(client, mock_db):
    """00:00–24:00 is open all day. A weekend with no slots still has a next open."""
    import asyncio
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bson import ObjectId

    asyncio.run(
        mock_db.businesses.insert_one(
            {
                "_id": ObjectId(),
                "name": "Ciavdar Grup Clinica Veterinară",
                "category": "veterinarian",
                "city": "Chișinău",
                "status": "published",
                "timezone": "Europe/Chisinau",
                "opening_hours": {
                    "mon": [{"open": "00:00", "close": "24:00"}],
                    "tue": [{"open": "00:00", "close": "24:00"}],
                    "wed": [{"open": "00:00", "close": "24:00"}],
                    "thu": [{"open": "00:00", "close": "24:00"}],
                    "fri": [{"open": "00:00", "close": "24:00"}],
                    "sat": [],
                    "sun": [],
                },
                "location": {"type": "Point", "coordinates": [28.86, 47.02]},
            }
        )
    )
    zone = ZoneInfo("Europe/Chisinau")

    class Clock(datetime):
        current = datetime(2026, 10, 3, 9, 41, tzinfo=zone)

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.current
            return cls.current.astimezone(tz)

    with patch("app.routers.businesses.datetime", Clock):
        saturday = client.get(
            "/api/v1/businesses/nearby?latitude=47.02&longitude=28.86",
            headers=HEADERS_A,
        )
        assert saturday.status_code == 200, saturday.text
        closed = saturday.json()["items"][0]
        assert closed["open_now"] is False
        assert closed["opens_at"] == "00:00"
        assert closed["next_open_day"] == "mon"

        Clock.current = datetime(2026, 9, 30, 12, 0, tzinfo=zone)
        weekday = client.get(
            "/api/v1/businesses/nearby?latitude=47.02&longitude=28.86",
            headers=HEADERS_A,
        )
        assert weekday.status_code == 200, weekday.text
        opened = weekday.json()["items"][0]
        assert opened["open_now"] is True
        assert opened["open_24_7"] is False
        assert opened["closes_at"] == "24:00"


def test_nearby_marks_all_day_every_day_as_open_24_7(client, mock_db):
    import asyncio

    from bson import ObjectId

    day = [{"open": "00:00", "close": "24:00"}]
    asyncio.run(
        mock_db.businesses.insert_one(
            {
                "_id": ObjectId(),
                "name": "Ciavdar Grup Clinica Veterinară",
                "category": "veterinarian",
                "city": "Chișinău",
                "status": "published",
                "timezone": "Europe/Chisinau",
                "opening_hours": {
                    "mon": day,
                    "tue": day,
                    "wed": day,
                    "thu": day,
                    "fri": day,
                    "sat": day,
                    "sun": day,
                },
                "location": {"type": "Point", "coordinates": [28.86, 47.02]},
            }
        )
    )
    listed = client.get(
        "/api/v1/businesses/nearby?latitude=47.02&longitude=28.86",
        headers=HEADERS_A,
    )
    assert listed.status_code == 200, listed.text
    place = listed.json()["items"][0]
    assert place["open_now"] is True
    assert place["open_24_7"] is True
    assert place["closes_at"] is None
    assert place["opens_at"] is None


def test_place_detail_returns_contact_hours_and_reviews(client, mock_db):
    import asyncio
    from datetime import datetime, timezone

    from bson import ObjectId

    doc_id = ObjectId()
    asyncio.run(
        mock_db.businesses.insert_one(
            {
                "_id": doc_id,
                "name": "Zoomama",
                "phone": ["+373 69 123 456", "+373 22 000 111"],
                "description": "A modern veterinary clinic.",
                "category": "veterinarian",
                "city": "Chișinău",
                "status": "published",
                "address": "Str. București 45",
                "timezone": "Europe/Chisinau",
                "opening_hours": {
                    "mon": [{"open": "09:00", "close": "19:00"}],
                    "tue": [],
                    "wed": [],
                    "thu": [],
                    "fri": [],
                    "sat": [],
                    "sun": [],
                },
                "website": "https://zoomama.md",
                "instagram": "@zoomama",
                "location": {"type": "Point", "coordinates": [28.83, 47.02]},
            }
        )
    )
    asyncio.run(
        mock_db.users.insert_one(
            {
                "firebase_uid": "uid_user_a",
                "name": "Laurie",
                "photo_url": "https://example.com/laurie.jpg",
            }
        )
    )
    asyncio.run(
        mock_db.business_reviews.insert_one(
            {
                "business_id": str(doc_id),
                "user_id": "uid_user_a",
                "rating": 5,
                "comment": "Very kind and caring staff.",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        )
    )
    response = client.get(
        f"/api/v1/businesses/{doc_id}?latitude=47.02&longitude=28.83",
        headers=HEADERS_A,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Zoomama"
    assert body["address"] == "Str. București 45"
    assert body["phone"] == ["+373 69 123 456", "+373 22 000 111"]
    assert body["website"] == "https://zoomama.md"
    assert body["instagram"] == "@zoomama"
    assert body["opening_hours"]["mon"][0]["open"] == "09:00"
    assert body["distance_km"] == 0.0
    assert body["rating"] == 5.0
    assert body["reviews"][0]["author_name"] == "Laurie"
    assert body["reviews"][0]["comment"] == "Very kind and caring staff."
    assert body["reviews"][0]["author_photo"] == "https://example.com/laurie.jpg"

    missing = client.get("/api/v1/businesses/not-an-id", headers=HEADERS_A)
    assert missing.status_code == 404


def test_owner_review_is_saved_once_and_updates_the_rating(client, mock_db):
    import asyncio

    from bson import ObjectId

    doc_id = ObjectId()
    asyncio.run(
        mock_db.businesses.insert_one(
            {
                "_id": doc_id,
                "name": "Zoomama",
                "category": "veterinarian",
                "city": "Chișinău",
                "status": "published",
                "address": "Str. București 45",
                "timezone": "Europe/Chisinau",
                "opening_hours": {"mon": [{"open": "09:00", "close": "19:00"}]},
            }
        )
    )
    asyncio.run(
        mock_db.users.insert_one(
            {"firebase_uid": "uid_user_a", "name": "Yoav", "photo_url": "https://example.com/yoav.jpg"}
        )
    )
    created = client.post(
        f"/api/v1/businesses/{doc_id}/reviews",
        headers=HEADERS_A,
        json={"rating": 4, "comment": "  Kind staff.  "},
    )
    assert created.status_code == 200, created.text
    assert created.json()["rating"] == 4
    assert created.json()["comment"] == "Kind staff."
    assert created.json()["author_name"] == "Yoav"
    assert created.json()["is_mine"] is True

    updated = client.post(
        f"/api/v1/businesses/{doc_id}/reviews",
        headers=HEADERS_A,
        json={"rating": 5, "comment": "Even better the second time."},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["id"] == created.json()["id"]
    assert mock_db.business_reviews._col.count_documents({"business_id": str(doc_id)}) == 1

    listed = client.get(f"/api/v1/businesses/{doc_id}", headers=HEADERS_A)
    body = listed.json()
    assert body["rating"] == 5.0
    assert body["reviews"][0]["comment"] == "Even better the second time."
    assert body["reviews"][0]["is_mine"] is True
