"""First-pet onboarding is stored on the user and survives the next login."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

from tests.conftest import HEADERS_A, USER_A_UID


def _seed_user(mock_db, **extra):
    async def seed():
        await mock_db.users.insert_one(
            {
                "firebase_uid": USER_A_UID,
                "email": "uid_user_a@test.com",
                "name": "Ada",
                "auth_provider": "email",
                "email_verified": True,
                "created_at": datetime.now(timezone.utc),
                **extra,
            }
        )

    asyncio.run(seed())


def test_onboarding_step_is_saved_and_returned(client, mock_db):
    _seed_user(mock_db)
    saved = client.patch(
        "/api/v1/users/me/onboarding",
        headers=HEADERS_A,
        json={
            "step": "photo",
            "pet_name": "Milo",
            "pet_type": "dog",
            "photo_url": None,
            "birth_date": None,
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["onboarding"]["step"] == "photo"
    assert body["onboarding"]["pet_name"] == "Milo"
    assert body["onboarding"]["pet_type"] == "dog"

    again = client.get("/api/v1/users/me", headers=HEADERS_A)
    assert again.status_code == 200
    assert again.json()["onboarding"]["step"] == "photo"
    assert again.json()["onboarding"]["pet_name"] == "Milo"


def test_later_step_without_a_name_falls_back_to_name(client, mock_db):
    _seed_user(mock_db)
    saved = client.patch(
        "/api/v1/users/me/onboarding",
        headers=HEADERS_A,
        json={"step": "photo", "pet_name": "  ", "pet_type": "cat"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["onboarding"]["step"] == "name"


def test_onboarding_photo_must_be_https(client, mock_db):
    _seed_user(mock_db)
    saved = client.patch(
        "/api/v1/users/me/onboarding",
        headers=HEADERS_A,
        json={
            "step": "birth",
            "pet_name": "Milo",
            "pet_type": "dog",
            "photo_url": "file:///tmp/pet.jpg",
        },
    )
    assert saved.status_code == 422


def test_login_keeps_the_same_account_and_the_draft(client, mock_db):
    _seed_user(
        mock_db,
        onboarding={
            "step": "photo",
            "pet_name": "Milo",
            "pet_type": "dog",
            "photo_url": None,
            "birth_date": None,
        },
    )
    logged_in = client.post("/api/v1/users/me", headers=HEADERS_A)
    assert logged_in.status_code == 200, logged_in.text
    assert logged_in.json()["onboarding"]["step"] == "photo"
    assert logged_in.json()["onboarding"]["pet_name"] == "Milo"
    stored = mock_db.users._col.find_one({"email": "uid_user_a@test.com"})
    assert stored["onboarding"]["step"] == "photo"


def test_verify_otp_reuses_the_started_account(client, mock_db):
    async def seed():
        await mock_db.users.insert_one(
            {
                "firebase_uid": "firebase_uid_existing",
                "email": "resume@test.com",
                "auth_provider": "email",
                "email_verified": True,
                "created_at": datetime.now(timezone.utc),
                "onboarding": {
                    "step": "photo",
                    "pet_name": "Milo",
                    "pet_type": "dog",
                    "photo_url": None,
                    "birth_date": None,
                },
            }
        )

    asyncio.run(seed())
    with patch("app.routers.auth.send_otp_email") as send_otp:
        with patch("app.routers.auth.firebase_auth.create_user") as create_user:
            with patch("app.routers.auth.firebase_auth.update_user") as update_user:
                with patch("app.routers.auth.firebase_auth.create_custom_token") as custom_token:
                    custom_token.return_value = b"token"
                    client.post("/api/v1/auth/send-otp", json={"email": "resume@test.com"})
                    otp_code = send_otp.call_args[0][1]
                    verified = client.post(
                        "/api/v1/auth/verify-otp",
                        json={"email": "resume@test.com", "otp": otp_code},
                    )

    assert verified.status_code == 200, verified.text
    create_user.assert_not_called()
    update_user.assert_called()
    rows = list(mock_db.users._col.find({"email": "resume@test.com"}))
    assert len(rows) == 1
    assert rows[0]["firebase_uid"] == "firebase_uid_existing"
    assert rows[0]["onboarding"]["step"] == "photo"
    assert rows[0]["onboarding"]["pet_name"] == "Milo"


def test_creating_the_pet_clears_onboarding(client, mock_db):
    _seed_user(
        mock_db,
        onboarding={
            "step": "birth",
            "pet_name": "Milo",
            "pet_type": "dog",
            "photo_url": "https://example.com/milo.jpg",
            "birth_date": "2020-01-02",
        },
    )
    created = client.post(
        "/api/v1/pets",
        headers=HEADERS_A,
        json={"name": "Milo", "type": "dog", "birth_date": "2020-01-02"},
    )
    assert created.status_code == 201, created.text
    profile = client.get("/api/v1/users/me", headers=HEADERS_A)
    assert profile.status_code == 200
    assert profile.json()["has_pets"] is True
    assert profile.json()["onboarding"] is None
    stored = mock_db.users._col.find_one({"firebase_uid": USER_A_UID})
    assert "onboarding" not in stored
