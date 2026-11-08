from unittest.mock import patch

from app.core.config import settings


def test_contact_sends_to_support(client, mock_db):
    with patch("app.routers.contact.send_review_report_email") as send:
        with patch.object(settings, "RAGLY_ADMIN_EMAILS", "admin@ragly.cloud"), patch.object(
            settings, "SUPPORT_EMAIL", "hello@ragly.cloud"
        ):
            res = client.post(
                "/api/v1/contact",
                json={"name": "Ana Clinic", "email": "ana@clinic.md", "phone": "+373 60 000 000"},
            )
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True}
    assert {call.args[0] for call in send.call_args_list} == {"admin@ragly.cloud", "hello@ragly.cloud"}
    assert send.call_args.kwargs["subject"] == "Ragly: website enquiry — Ana Clinic"
    mailed = "\n".join(send.call_args.kwargs["lines"])
    assert "ana@clinic.md" in mailed
    assert "+373 60 000 000" in mailed


def test_contact_honeypot_is_silent(client, mock_db):
    with patch("app.routers.contact.send_review_report_email") as send:
        res = client.post(
            "/api/v1/contact",
            json={"name": "Bot", "email": "bot@spam.test", "website": "https://spam.test"},
        )
    assert res.status_code == 200
    send.assert_not_called()


def test_contact_rejects_bad_email(client, mock_db):
    res = client.post("/api/v1/contact", json={"name": "Ana", "email": "not-an-email"})
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "invalid_email"


def test_contact_cooldown_per_email(client, mock_db):
    with patch("app.routers.contact.send_review_report_email"):
        first = client.post("/api/v1/contact", json={"name": "Ana Clinic", "email": "ana@clinic.md"})
        second = client.post("/api/v1/contact", json={"name": "Ana Clinic", "email": "ana@clinic.md"})
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "contact_too_soon"
