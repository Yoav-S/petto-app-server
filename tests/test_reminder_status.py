"""
test_reminder_status.py — Reminder status computation and tab filtering.

Rules being tested (server-rules §3.3):
  Server computes: Scheduled | Today | Missed | Completed

  stored_status = "scheduled" + date == today → returned status = "today"
  stored_status = "scheduled" + date >  today → returned status = "scheduled"
  stored_status = "scheduled" + date <  today → returned status = "missed"
  PATCH .../status completed → returned status = "completed"
  PATCH .../status missed    → returned status = "missed"

Tab filtering:
  today    → date == today, scheduled
  upcoming → date >  today, scheduled
  recent   → completed | missed | (date < today AND scheduled)

Create/update API rejects past dates (today is always allowed).
future reminder, then backdate the stored date.
the document in the DB.
"""
import pytest
from datetime import date, timedelta
from bson import ObjectId
from tests.conftest import HEADERS_A, make_pet


def today() -> str:
    return date.today().isoformat()


def future(days: int = 30) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def past(days: int = 1) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def create_reminder(client, mock_db, pet_id: str, reminder_date: str, **kwargs) -> dict:
    """Create via API (always tomorrow+), then optionally backdate for status tests."""
    time = kwargs.pop("time", "09:00")
    title = kwargs.pop("title", "Test")
    payload = {"title": title, "date": future(60), "time": time, **kwargs}
    r = client.post(f"/api/v1/pets/{pet_id}/reminders", json=payload, headers=HEADERS_A)
    assert r.status_code == 201, r.text
    reminder = r.json()

    if reminder_date != payload["date"]:
        mock_db.reminders._col.update_one(
            {"_id": ObjectId(reminder["id"])},
            {"$set": {"date": reminder_date}},
        )
        get_r = client.get(
            f"/api/v1/pets/{pet_id}/reminders/{reminder['id']}",
            headers=HEADERS_A,
        )
        assert get_r.status_code == 200, get_r.text
        return get_r.json()

    return reminder


class TestReminderStatusComputation:
    """Server computes the correct display status from date + stored_status."""

    def test_future_date_returns_scheduled(self, client, mock_db):
        pet = make_pet(client, HEADERS_A)
        reminder = create_reminder(client, mock_db, pet["id"], future())
        assert reminder["status"] == "scheduled"

    def test_today_date_returns_today(self, client, mock_db):
        pet = make_pet(client, HEADERS_A)
        reminder = create_reminder(client, mock_db, pet["id"], today(), time="23:59")
        assert reminder["status"] == "today"

    def test_past_date_without_action_returns_missed(self, client, mock_db):
        """A past-due reminder with stored_status=scheduled → returned as missed."""
        pet = make_pet(client, HEADERS_A)
        reminder = create_reminder(client, mock_db, pet["id"], past(5))
        assert reminder["status"] == "missed"

    def test_patch_completed_returns_completed(self, client, mock_db):
        pet = make_pet(client, HEADERS_A)
        reminder = create_reminder(client, mock_db, pet["id"], today(), time="23:59")
        r = client.patch(
            f"/api/v1/pets/{pet['id']}/reminders/{reminder['id']}/status",
            json={"status": "completed"},
            headers=HEADERS_A,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "completed"

    def test_patch_missed_returns_missed(self, client, mock_db):
        pet = make_pet(client, HEADERS_A)
        reminder = create_reminder(client, mock_db, pet["id"], today(), time="23:59")
        r = client.patch(
            f"/api/v1/pets/{pet['id']}/reminders/{reminder['id']}/status",
            json={"status": "missed"},
            headers=HEADERS_A,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "missed"

    def test_invalid_status_value_rejected(self, client, mock_db):
        """Status must be 'completed' or 'missed' — anything else is 422."""
        pet = make_pet(client, HEADERS_A)
        reminder = create_reminder(client, mock_db, pet["id"], today())
        r = client.patch(
            f"/api/v1/pets/{pet['id']}/reminders/{reminder['id']}/status",
            json={"status": "done"},  # not in enum
            headers=HEADERS_A,
        )
        assert r.status_code == 422


class TestReminderTabFiltering:
    """Each tab returns only the correct subset of reminders."""

    def test_today_tab_returns_only_todays_scheduled(self, client, mock_db):
        pet = make_pet(client, HEADERS_A)
        # Late time so the row stays on Today regardless of wall clock.
        create_reminder(client, mock_db, pet["id"], today(), time="23:59")
        create_reminder(client, mock_db, pet["id"], future(), time="10:00")
        create_reminder(client, mock_db, pet["id"], past(), time="11:00")

        r = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=today", headers=HEADERS_A
        )
        items = r.json()
        assert len(items) == 1
        assert items[0]["status"] == "today"

    def test_upcoming_tab_returns_only_future_scheduled(self, client, mock_db):
        pet = make_pet(client, HEADERS_A)
        create_reminder(client, mock_db, pet["id"], today(), time="23:59")
        create_reminder(client, mock_db, pet["id"], future(10), time="10:00")
        create_reminder(client, mock_db, pet["id"], future(20), time="11:00")

        r = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=upcoming", headers=HEADERS_A
        )
        items = r.json()
        assert len(items) == 2
        # Sorted soonest first
        assert items[0]["date"] < items[1]["date"]

    def test_recent_tab_returns_completed_and_missed(self, client, mock_db):
        pet = make_pet(client, HEADERS_A)
        r1 = create_reminder(client, mock_db, pet["id"], today(), time="23:59")
        create_reminder(client, mock_db, pet["id"], past(3), time="10:00")  # auto-missed

        # Explicitly mark r1 as completed
        client.patch(
            f"/api/v1/pets/{pet['id']}/reminders/{r1['id']}/status",
            json={"status": "completed"},
            headers=HEADERS_A,
        )

        r = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=recent", headers=HEADERS_A
        )
        statuses = {item["status"] for item in r.json()}
        assert "completed" in statuses
        assert "missed" in statuses

    def test_today_past_time_moves_to_recent(self, client, mock_db):
        """Same-day reminder whose clock time already passed → Recent as missed."""
        pet = make_pet(client, HEADERS_A)
        create_reminder(client, mock_db, pet["id"], today(), time="00:00")

        today_items = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=today", headers=HEADERS_A
        ).json()
        assert today_items == []

        recent_items = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=recent", headers=HEADERS_A
        ).json()
        assert len(recent_items) == 1
        assert recent_items[0]["status"] == "missed"

    def test_today_tab_excludes_completed_reminders(self, client, mock_db):
        """A completed reminder for today must NOT appear in today tab."""
        pet = make_pet(client, HEADERS_A)
        r1 = create_reminder(client, mock_db, pet["id"], today(), time="23:59")
        client.patch(
            f"/api/v1/pets/{pet['id']}/reminders/{r1['id']}/status",
            json={"status": "completed"},
            headers=HEADERS_A,
        )
        r = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=today", headers=HEADERS_A
        )
        assert r.json() == []

    def test_empty_today_tab_returns_empty_list(self, client):
        pet = make_pet(client, HEADERS_A)
        r = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=today", headers=HEADERS_A
        )
        assert r.status_code == 200
        assert r.json() == []
