"""Tests for auth registration and OTP verification."""
from unittest.mock import patch, MagicMock


def test_register_sends_otp(client):
    with patch("app.routers.auth.send_otp_email") as send_otp:
        r = client.post(
            "/api/v1/auth/register",
            json={"email": "newuser@test.com", "password": "securepass1"},
        )
    assert r.status_code == 201, r.text
    send_otp.assert_called_once()
    otp_code = send_otp.call_args[0][1]
    assert len(otp_code) == 4


def test_verify_otp_activates_user(client, mock_db):
    with patch("app.routers.auth.send_otp_email") as send_otp:
        with patch("app.routers.auth.firebase_auth.create_user") as create_user:
            create_user.return_value = MagicMock(uid="firebase_uid_new")
            client.post(
                "/api/v1/auth/register",
                json={"email": "verify@test.com", "password": "securepass1"},
            )
            otp_code = send_otp.call_args[0][1]

            r = client.post(
                "/api/v1/auth/verify-otp",
                json={
                    "email": "verify@test.com",
                    "otp": otp_code,
                    "password": "securepass1",
                },
            )

    assert r.status_code == 200, r.text
    create_user.assert_called_once()


def test_register_duplicate_verified_email(client):
    with patch("app.routers.auth.send_otp_email"):
        payload = {"email": "dup@test.com", "password": "securepass1"}
        r1 = client.post("/api/v1/auth/register", json=payload)
        assert r1.status_code == 201

        with patch("app.routers.auth.firebase_auth.create_user") as create_user:
            create_user.return_value = MagicMock(uid="uid_dup")
            otp = "0000"
            with patch("app.routers.auth.verify_otp_code", return_value=True):
                client.post(
                    "/api/v1/auth/verify-otp",
                    json={"email": "dup@test.com", "otp": otp, "password": "securepass1"},
                )

        r2 = client.post("/api/v1/auth/register", json=payload)
        assert r2.status_code == 409


def test_users_me_updates_last_login(client):
    r = client.post("/api/v1/users/me", headers={"Authorization": "Bearer token_user_a"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["email"] == "uid_user_a@test.com"
    assert data["last_login_at"] is not None

    r2 = client.post("/api/v1/users/me", headers={"Authorization": "Bearer token_user_a"})
    assert r2.status_code == 200
    assert r2.json()["id"] == data["id"]
