"""
test_validation.py — Required fields, date formats, and text length limits.

Rule being tested (server-rules §6.3):
  - Required fields missing → 422
  - Invalid date format → 422
  - text length > 300 → 422
  - Invalid enum values → 422

FastAPI/Pydantic returns 422 Unprocessable Entity for schema violations.
"""
import pytest
from tests.conftest import HEADERS_A, make_pet


class TestPetValidation:
    """Pet creation requires name and type."""

    def test_missing_name_rejected(self, client):
        r = client.post("/api/v1/pets", json={"type": "Dog"}, headers=HEADERS_A)
        assert r.status_code == 422

    def test_missing_type_rejected(self, client):
        r = client.post("/api/v1/pets", json={"name": "Buddy"}, headers=HEADERS_A)
        assert r.status_code == 422

    def test_notes_over_300_chars_rejected(self, client):
        r = client.post(
            "/api/v1/pets",
            json={"name": "Buddy", "type": "Dog", "notes": "x" * 301},
            headers=HEADERS_A,
        )
        assert r.status_code == 422


class TestReminderValidation:
    """Reminder requires title, date, and time in correct format."""

    def test_missing_title_rejected(self, client):
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={"date": "2099-01-01", "time": "09:00"},
            headers=HEADERS_A,
        )
        assert r.status_code == 422

    def test_missing_date_rejected(self, client):
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={"title": "Test", "time": "09:00"},
            headers=HEADERS_A,
        )
        assert r.status_code == 422

    def test_invalid_time_format_rejected(self, client):
        """Time must be HH:MM, not freeform."""
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={"title": "Test", "date": "2099-01-01", "time": "9am"},
            headers=HEADERS_A,
        )
        assert r.status_code == 422

    def test_invalid_repeat_value_rejected(self, client):
        """repeat must be one of the defined enum values."""
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={
                "title": "Test",
                "date": "2099-01-01",
                "time": "09:00",
                "repeat": "every_hour",  # not in enum
            },
            headers=HEADERS_A,
        )
        assert r.status_code == 422

    def test_title_over_300_chars_rejected(self, client):
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={"title": "x" * 301, "date": "2099-01-01", "time": "09:00"},
            headers=HEADERS_A,
        )
        assert r.status_code == 422

    def test_duplicate_datetime_allowed(self, client):
        """Multiple reminders may share the same date+time on one pet."""
        pet = make_pet(client, HEADERS_A)
        payload = {"title": "Morning meds", "date": "2099-06-01", "time": "09:00"}
        first = client.post(
            f"/api/v1/pets/{pet['id']}/reminders", json=payload, headers=HEADERS_A
        )
        assert first.status_code == 201
        second = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={"title": "Other task", "date": "2099-06-01", "time": "09:00"},
            headers=HEADERS_A,
        )
        assert second.status_code == 201
        assert second.json()["id"] != first.json()["id"]

    def test_past_datetime_rejected(self, client):
        """Cannot schedule a reminder in the past."""
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={"title": "Too late", "date": "2000-01-01", "time": "09:00"},
            headers=HEADERS_A,
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "reminder_datetime_in_past"

    def test_today_future_time_accepted(self, client):
        """Today is allowed when the local time is still in the future."""
        from datetime import date, datetime

        pet = make_pet(client, HEADERS_A)
        now = datetime.now()
        if now.hour == 23 and now.minute >= 59:
            return
        r = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={"title": "Later today", "date": date.today().isoformat(), "time": "23:59"},
            headers=HEADERS_A,
        )
        assert r.status_code == 201

    def test_today_past_time_accepted(self, client):
        """Today is allowed even when the chosen time has already passed."""
        from datetime import date

        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={"title": "Today anyway", "date": date.today().isoformat(), "time": "00:00"},
            headers=HEADERS_A,
        )
        assert r.status_code == 201

    def test_end_date_not_after_start_rejected(self, client):
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={
                "title": "Walk",
                "date": "2099-06-10",
                "time": "09:00",
                "end_date": "2099-06-10",
            },
            headers=HEADERS_A,
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "end_date_before_start"

    def test_end_date_and_alert_accepted(self, client):
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={
                "title": "Walk",
                "date": "2099-06-10",
                "time": "09:00",
                "repeat": "every_day",
                "end_date": "2099-06-20",
                "alert": "15m",
            },
            headers=HEADERS_A,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["end_date"] == "2099-06-20"
        assert body["alert"] == "15m"

    def test_invalid_alert_rejected(self, client):
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={
                "title": "Walk",
                "date": "2099-06-10",
                "time": "09:00",
                "alert": "3m",
            },
            headers=HEADERS_A,
        )
        assert r.status_code == 422


class TestVaccinationValidation:
    """Vaccination requires name and date."""

    def test_missing_name_rejected(self, client):
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/vaccinations",
            json={"date": "2025-01-01"},
            headers=HEADERS_A,
        )
        assert r.status_code == 422

    def test_missing_date_rejected(self, client):
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/vaccinations",
            json={"name": "Rabies"},
            headers=HEADERS_A,
        )
        assert r.status_code == 422


class TestHealthNoteValidation:
    """HealthNote text is required and has max 300 chars."""

    def test_missing_text_rejected(self, client):
        pet = make_pet(client, HEADERS_A)
        r_record = client.post(
            f"/api/v1/pets/{pet['id']}/medical-records",
            json={"title": "Allergy"},
            headers=HEADERS_A,
        )
        record_id = r_record.json()["id"]
        r = client.post(
            f"/api/v1/pets/{pet['id']}/medical-records/{record_id}/notes",
            json={},
            headers=HEADERS_A,
        )
        assert r.status_code == 422

    def test_text_over_300_chars_rejected(self, client):
        pet = make_pet(client, HEADERS_A)
        r_record = client.post(
            f"/api/v1/pets/{pet['id']}/medical-records",
            json={"title": "Allergy"},
            headers=HEADERS_A,
        )
        record_id = r_record.json()["id"]
        r = client.post(
            f"/api/v1/pets/{pet['id']}/medical-records/{record_id}/notes",
            json={"text": "x" * 301},
            headers=HEADERS_A,
        )
        assert r.status_code == 422


class TestMedicalRecordStatusValidation:
    """Status PATCH accepts active ↔ resolved."""

    def test_invalid_status_value_rejected(self, client):
        pet = make_pet(client, HEADERS_A)
        r_record = client.post(
            f"/api/v1/pets/{pet['id']}/medical-records",
            json={"title": "Allergy"},
            headers=HEADERS_A,
        )
        record_id = r_record.json()["id"]
        r = client.patch(
            f"/api/v1/pets/{pet['id']}/medical-records/{record_id}/status",
            json={"status": "pending"},
            headers=HEADERS_A,
        )
        assert r.status_code == 422

    def test_reopen_resolved_record(self, client):
        pet = make_pet(client, HEADERS_A)
        r_record = client.post(
            f"/api/v1/pets/{pet['id']}/medical-records",
            json={"title": "Allergy"},
            headers=HEADERS_A,
        )
        record_id = r_record.json()["id"]
        client.patch(
            f"/api/v1/pets/{pet['id']}/medical-records/{record_id}/status",
            json={"status": "resolved"},
            headers=HEADERS_A,
        )
        r = client.patch(
            f"/api/v1/pets/{pet['id']}/medical-records/{record_id}/status",
            json={"status": "active"},
            headers=HEADERS_A,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "active"

    def test_reopening_active_record_rejected(self, client):
        pet = make_pet(client, HEADERS_A)
        r_record = client.post(
            f"/api/v1/pets/{pet['id']}/medical-records",
            json={"title": "Allergy"},
            headers=HEADERS_A,
        )
        record_id = r_record.json()["id"]
        r = client.patch(
            f"/api/v1/pets/{pet['id']}/medical-records/{record_id}/status",
            json={"status": "active"},
            headers=HEADERS_A,
        )
        assert r.status_code == 422

    def test_resolving_already_resolved_record_rejected(self, client):
        pet = make_pet(client, HEADERS_A)
        r_record = client.post(
            f"/api/v1/pets/{pet['id']}/medical-records",
            json={"title": "Allergy"},
            headers=HEADERS_A,
        )
        record_id = r_record.json()["id"]
        # First resolve
        client.patch(
            f"/api/v1/pets/{pet['id']}/medical-records/{record_id}/status",
            json={"status": "resolved"},
            headers=HEADERS_A,
        )
        # Second resolve attempt
        r = client.patch(
            f"/api/v1/pets/{pet['id']}/medical-records/{record_id}/status",
            json={"status": "resolved"},
            headers=HEADERS_A,
        )
        assert r.status_code == 422
