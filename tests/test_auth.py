"""Tests for passwordless OTP auth."""
from unittest.mock import patch, MagicMock


def test_resend_sends_otp_email():
    with patch(
        "app.core.email_service.resolve_resend_credentials",
        return_value=("re_test_key", "onboarding@resend.dev", "env"),
    ):
        with patch("app.core.email_service.httpx.post") as post:
            post.return_value = MagicMock(status_code=200, json=lambda: {"id": "msg-abc"})
            from app.core.email_service import send_otp_email

            send_otp_email("user@test.com", "123456")

            post.assert_called_once()
            payload = post.call_args.kwargs["json"]
            assert payload["from"] == "onboarding@resend.dev"
            assert payload["to"] == ["user@test.com"]
            assert "123456" in payload["text"]
            assert "123456" in payload["html"]


def test_send_otp_creates_pending_user(client):
    with patch("app.routers.auth.send_otp_email") as send_otp:
        r = client.post("/api/v1/auth/send-otp", json={"email": "new@test.com"})
    assert r.status_code == 200, r.text
    send_otp.assert_called_once()
    assert len(send_otp.call_args[0][1]) == 6


def test_send_otp_second_user_does_not_500(client):
    """Regression: unique sparse firebase_uid index breaks on firebase_uid: null."""
    with patch("app.routers.auth.send_otp_email"):
        r1 = client.post("/api/v1/auth/send-otp", json={"email": "first@test.com"})
        r2 = client.post("/api/v1/auth/send-otp", json={"email": "second@test.com"})
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text


def test_verify_otp_returns_custom_token(client):
    with patch("app.routers.auth.send_otp_email") as send_otp:
        with patch("app.routers.auth.firebase_auth.create_user") as create_user:
            with patch("app.routers.auth.firebase_auth.create_custom_token") as custom_token:
                create_user.return_value = MagicMock(uid="firebase_uid_new")
                custom_token.return_value = b"custom-token-abc"

                client.post("/api/v1/auth/send-otp", json={"email": "verify@test.com"})
                otp_code = send_otp.call_args[0][1]

                r = client.post(
                    "/api/v1/auth/verify-otp",
                    json={"email": "verify@test.com", "otp": otp_code},
                )

    assert r.status_code == 200, r.text
    assert r.json()["custom_token"] == "custom-token-abc"


def test_verify_otp_updates_mongo_user(client, mock_db):
    with patch("app.routers.auth.send_otp_email") as send_otp:
        with patch("app.routers.auth.firebase_auth.create_user") as create_user:
            with patch("app.routers.auth.firebase_auth.create_custom_token") as custom_token:
                create_user.return_value = MagicMock(uid="firebase_uid_verified")
                custom_token.return_value = b"token"

                client.post("/api/v1/auth/send-otp", json={"email": "mongo@test.com"})
                otp_code = send_otp.call_args[0][1]

                pending = mock_db.users._col.find_one({"email": "mongo@test.com"})
                assert pending is not None
                assert pending.get("email_verified") is False
                assert "firebase_uid" not in pending

                r = client.post(
                    "/api/v1/auth/verify-otp",
                    json={"email": "mongo@test.com", "otp": otp_code},
                )
                assert r.status_code == 200, r.text

                verified = mock_db.users._col.find_one({"email": "mongo@test.com"})
                assert verified["firebase_uid"] == "firebase_uid_verified"
                assert verified["email_verified"] is True
                assert verified["last_login_at"] is not None
                assert mock_db.email_otps._col.find_one({"email": "mongo@test.com"}) is None


def test_users_me_rejects_unverified_email_user(client, mock_db):
    import asyncio

    async def seed():
        await mock_db.users.insert_one(
            {
                "firebase_uid": "uid_unverified",
                "email": "pending@test.com",
                "auth_provider": "email",
                "email_verified": False,
            }
        )

    asyncio.get_event_loop().run_until_complete(seed())

    with patch("app.middleware.auth.verify_firebase_token") as verify:
        verify.return_value = {
            "uid": "uid_unverified",
            "email": "pending@test.com",
            "token": {"firebase": {"sign_in_provider": "custom"}},
        }
        r = client.post("/api/v1/users/me", headers={"Authorization": "Bearer any"})

    assert r.status_code == 403


def test_verify_otp_accepts_leading_zero_code(client):
    """OTP codes like 027485 must verify as strings, not lose a leading zero."""
    with patch("app.routers.auth.send_otp_email") as send_otp:
        with patch("app.routers.auth.generate_otp_code", return_value="027485"):
            with patch("app.routers.auth.firebase_auth.create_user") as create_user:
                with patch("app.routers.auth.firebase_auth.create_custom_token") as custom_token:
                    create_user.return_value = MagicMock(uid="uid_leading_zero")
                    custom_token.return_value = b"token-lz"

                    client.post("/api/v1/auth/send-otp", json={"email": "leading@test.com"})
                    send_otp.assert_called_with("leading@test.com", "027485")

                    r = client.post(
                        "/api/v1/auth/verify-otp",
                        json={"email": "leading@test.com", "otp": "027485"},
                    )

    assert r.status_code == 200, r.text
    assert r.json()["custom_token"] == "token-lz"


def test_resend_otp_respects_cooldown(client):
    with patch("app.routers.auth.send_otp_email"):
        client.post("/api/v1/auth/send-otp", json={"email": "cooldown@test.com"})
        r2 = client.post("/api/v1/auth/resend-otp", json={"email": "cooldown@test.com"})
    assert r2.status_code == 429


def test_users_me_updates_last_login(client):
    r = client.post("/api/v1/users/me", headers={"Authorization": "Bearer token_user_a"})
    assert r.status_code == 200, r.text
    assert r.json()["last_login_at"] is not None
