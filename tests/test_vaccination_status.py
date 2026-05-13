"""
test_vaccination_status.py — Computed vaccination status correctness.

Rule being tested (server-rules §3.3):
  Server MUST calculate vaccination status:
    up_to_date → next_date null OR > today + 30 days
    due_soon   → next_date within 30 days
    overdue    → next_date < today

Also tests auto-reminder creation when next_date is provided (server-rules §3.2).
"""
import pytest
from datetime import date, timedelta
from tests.conftest import HEADERS_A, make_pet


def today_plus(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def today_minus(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


class TestVaccinationStatus:
    """compute_vaccination_status() via the API."""

    def test_no_next_date_is_up_to_date(self, client):
        """Vaccination without next_date → up_to_date."""
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/vaccinations",
            json={"name": "Rabies", "date": today_minus(30)},
            headers=HEADERS_A,
        )
        assert r.status_code == 201
        assert r.json()["status"] == "up_to_date"

    def test_next_date_far_future_is_up_to_date(self, client):
        """next_date > 30 days away → up_to_date."""
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/vaccinations",
            json={"name": "Rabies", "date": today_minus(365), "next_date": today_plus(60)},
            headers=HEADERS_A,
        )
        assert r.json()["status"] == "up_to_date"

    def test_next_date_within_30_days_is_due_soon(self, client):
        """next_date ≤ 30 days from today → due_soon."""
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/vaccinations",
            json={"name": "Rabies", "date": today_minus(365), "next_date": today_plus(15)},
            headers=HEADERS_A,
        )
        assert r.json()["status"] == "due_soon"

    def test_next_date_in_past_is_overdue(self, client):
        """next_date before today → overdue."""
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/vaccinations",
            json={"name": "Rabies", "date": today_minus(365), "next_date": today_minus(1)},
            headers=HEADERS_A,
        )
        assert r.json()["status"] == "overdue"

    def test_next_date_today_is_due_soon(self, client):
        """next_date == today → due_soon (boundary: 0 days away ≤ 30)."""
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/vaccinations",
            json={"name": "Rabies", "date": today_minus(365), "next_date": date.today().isoformat()},
            headers=HEADERS_A,
        )
        assert r.json()["status"] == "due_soon"

    def test_status_recomputed_after_next_date_update(self, client):
        """Updating next_date to the past should return overdue on next read."""
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/vaccinations",
            json={"name": "Rabies", "date": today_minus(365), "next_date": today_plus(60)},
            headers=HEADERS_A,
        )
        vac_id = r.json()["id"]
        assert r.json()["status"] == "up_to_date"

        # Update next_date to the past
        r2 = client.patch(
            f"/api/v1/pets/{pet['id']}/vaccinations/{vac_id}",
            json={"next_date": today_minus(5)},
            headers=HEADERS_A,
        )
        assert r2.json()["status"] == "overdue"


class TestVaccinationAutoReminder:
    """Auto-reminder creation when next_date is set (server-rules §3.2)."""

    def test_auto_reminder_created_on_next_date(self, client):
        """Creating a vaccination with next_date creates a linked reminder."""
        pet = make_pet(client, HEADERS_A)
        next_date = today_plus(60)
        client.post(
            f"/api/v1/pets/{pet['id']}/vaccinations",
            json={"name": "Bordetella", "date": today_minus(365), "next_date": next_date},
            headers=HEADERS_A,
        )
        # The reminder should appear in upcoming tab
        r = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=upcoming", headers=HEADERS_A
        )
        reminders = r.json()
        assert len(reminders) == 1
        assert "Bordetella" in reminders[0]["title"]
        assert reminders[0]["date"] == next_date
        assert reminders[0]["time"] == "09:00"

    def test_no_auto_reminder_without_next_date(self, client):
        """Creating a vaccination without next_date must NOT create a reminder."""
        pet = make_pet(client, HEADERS_A)
        client.post(
            f"/api/v1/pets/{pet['id']}/vaccinations",
            json={"name": "Rabies", "date": today_minus(30)},
            headers=HEADERS_A,
        )
        r = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=upcoming", headers=HEADERS_A
        )
        assert r.json() == []
