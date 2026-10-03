"""Account profile fields and email change."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from firebase_admin.auth import UserNotFoundError

from app.core.otp import hash_otp
from tests.conftest import HEADERS_A, USER_A_UID


def _seed_user(mock_db):
    async def seed():
        await mock_db.users.insert_one(
            {
                "firebase_uid": USER_A_UID,
                "email": "uid_user_a@test.com",
                "name": "Ada",
                "auth_provider": "email",
                "email_verified": True,
                "created_at": datetime.now(timezone.utc),
            }
        )

    asyncio.run(seed())


def test_patch_saves_name_phone_and_photo(client, mock_db):
    _seed_user(mock_db)
    response = client.patch(
        "/api/v1/users/me",
        headers=HEADERS_A,
        json={
            "name": "Ada Lovelace",
            "phone": " +373 60 000 ",
            "photo_url": "https://example.com/a.jpg",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Ada Lovelace"
    assert body["phone"] == "+373 60 000"
    assert body["photo_url"] == "https://example.com/a.jpg"

    name_only = client.patch(
        "/api/v1/users/me",
        headers=HEADERS_A,
        json={"name": "Ada"},
    )
    assert name_only.status_code == 200
    kept = name_only.json()
    assert kept["phone"] == "+373 60 000"
    assert kept["photo_url"] == "https://example.com/a.jpg"


def test_email_change_requires_otp_and_rejects_taken_address(client, mock_db):
    _seed_user(mock_db)
    async def seed_other():
        await mock_db.users.insert_one(
            {
                "firebase_uid": "someone_else",
                "email": "taken@example.com",
                "auth_provider": "email",
                "email_verified": True,
                "created_at": datetime.now(timezone.utc),
            }
        )

    asyncio.run(seed_other())
    taken = client.post(
        "/api/v1/users/me/email/otp",
        headers=HEADERS_A,
        json={"email": "taken@example.com"},
    )
    assert taken.status_code == 409
    assert taken.json()["detail"]["code"] == "email_in_use"

    with patch("app.routers.auth.send_otp_email") as send_mail:
        with patch(
            "app.routers.users.firebase_auth.get_user_by_email",
            side_effect=UserNotFoundError("missing"),
        ):
            sent = client.post(
                "/api/v1/users/me/email/otp",
                headers=HEADERS_A,
                json={"email": "fresh@example.com"},
            )
    assert sent.status_code == 200
    send_mail.assert_called_once()
    stored = mock_db.email_otps._col.find_one({"email": "fresh@example.com"})
    assert stored["purpose"] == "email_change"
    assert stored["firebase_uid"] == USER_A_UID
    assert mock_db.users._col.find_one({"firebase_uid": USER_A_UID})["email"] == "uid_user_a@test.com"


def test_confirm_email_updates_same_uid(client, mock_db):
    _seed_user(mock_db)
    async def seed_otp():
        await mock_db.email_otps.insert_one(
            {
                "email": "fresh@example.com",
                "otp_hash": hash_otp("123456"),
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
                "attempts": 0,
                "purpose": "email_change",
                "firebase_uid": USER_A_UID,
            }
        )

    asyncio.run(seed_otp())
    with patch("app.routers.users.firebase_auth.update_user") as update_user:
        with patch(
            "app.routers.users.firebase_auth.get_user_by_email",
            side_effect=UserNotFoundError("missing"),
        ):
            response = client.post(
                "/api/v1/users/me/email/confirm",
                headers=HEADERS_A,
                json={"email": "fresh@example.com", "otp": "123456"},
            )
    assert response.status_code == 200
    assert response.json()["email"] == "fresh@example.com"
    update_user.assert_called_once_with(USER_A_UID, email="fresh@example.com", email_verified=True)
    assert mock_db.users._col.find_one({"firebase_uid": USER_A_UID})["email"] == "fresh@example.com"
    assert mock_db.email_otps._col.find_one({"email": "fresh@example.com"}) is None
