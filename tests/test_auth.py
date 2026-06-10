"""Tests for passwordless OTP auth."""
from unittest.mock import patch, MagicMock


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


def test_resend_otp_respects_cooldown(client):
    with patch("app.routers.auth.send_otp_email"):
        client.post("/api/v1/auth/send-otp", json={"email": "cooldown@test.com"})
        r2 = client.post("/api/v1/auth/resend-otp", json={"email": "cooldown@test.com"})
    assert r2.status_code == 429


def test_users_me_updates_last_login(client):
    r = client.post("/api/v1/users/me", headers={"Authorization": "Bearer token_user_a"})
    assert r.status_code == 200, r.text
    assert r.json()["last_login_at"] is not None
