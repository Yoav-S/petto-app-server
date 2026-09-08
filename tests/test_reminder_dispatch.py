"""Dispatcher: alert vs main push copy, and repeating occurrence spawn."""
from datetime import datetime, timezone
from unittest.mock import patch

from app.core.config import settings
from tests.conftest import HEADERS_A, USER_A_UID, make_pet
from tests.test_reminder_status import create_reminder, future, today, past

SECRET = "test-dispatch-secret"


def post_dispatch(client, **params):
    url = "/api/v1/internal/dispatch-reminders"
    if params:
        query = "&".join(f"{key}={value}" for key, value in params.items())
        url = f"{url}?{query}"
    with patch.object(settings, "INTERNAL_TASK_SECRET", SECRET):
        return client.post(url, headers={"X-Internal-Secret": SECRET})


class TestReminderDispatchSeries:
    def test_main_fire_keeps_row_and_spawns_next(self, client, mock_db):
        pet = make_pet(client, HEADERS_A)
        reminder = create_reminder(
            client,
            mock_db,
            pet["id"],
            today(),
            time="00:00",
            repeat="every_day",
            title="Walk",
        )

        r = post_dispatch(client)
        assert r.status_code == 200, r.text
        assert r.json()["due"] >= 1

        row = client.get(
            f"/api/v1/pets/{pet['id']}/reminders/{reminder['id']}",
            headers=HEADERS_A,
        ).json()
        assert row["date"] == today()
        assert row["awaiting_ack"] is True

        recent = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=recent", headers=HEADERS_A
        ).json()
        upcoming = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=upcoming", headers=HEADERS_A
        ).json()
        assert any(item["id"] == reminder["id"] for item in recent)
        assert len(upcoming) == 1
        assert upcoming[0]["date"] == future(1)
        assert upcoming[0]["title"] == "Walk"
        assert upcoming[0]["id"] != reminder["id"]

    def test_dispatch_does_not_duplicate_next_occurrence(self, client, mock_db):
        pet = make_pet(client, HEADERS_A)
        create_reminder(
            client,
            mock_db,
            pet["id"],
            today(),
            time="00:00",
            repeat="every_day",
            title="Walk",
        )
        post_dispatch(client)
        post_dispatch(client)
        upcoming = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=upcoming", headers=HEADERS_A
        ).json()
        assert len(upcoming) == 1

    def test_missed_days_are_materialized(self, client, mock_db):
        pet = make_pet(client, HEADERS_A)
        start = past(3)
        reminder = create_reminder(
            client,
            mock_db,
            pet["id"],
            start,
            time="00:00",
            repeat="every_day",
            title="Walk",
        )

        r = post_dispatch(client)
        assert r.status_code == 200, r.text

        recent = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=recent", headers=HEADERS_A
        ).json()
        upcoming = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=upcoming", headers=HEADERS_A
        ).json()
        recent_dates = {item["date"] for item in recent}
        assert start in recent_dates
        assert today() in recent_dates
        assert len(upcoming) == 1
        assert upcoming[0]["date"] == future(1)
        original = next(item for item in recent if item["id"] == reminder["id"])
        assert original["date"] == start

    def test_alert_push_title_is_alert(self, client, mock_db):
        pet = make_pet(client, HEADERS_A)
        create_reminder(
            client,
            mock_db,
            pet["id"],
            today(),
            time="00:00",
            alert="5m",
            title="Give pills",
        )
        mock_db.push_tokens._col.insert_one(
            {
                "token": "ExponentPushToken[test]",
                "user_id": USER_A_UID,
                "platform": "ios",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        captured: list[dict] = []

        async def capture(messages):
            captured.extend(messages)
            return [{"status": "ok"} for _ in messages]

        with patch("app.routers.notifications.send_expo_push", side_effect=capture):
            r = post_dispatch(client)
        assert r.status_code == 200, r.text

        by_kind = {msg["data"]["kind"]: msg for msg in captured}
        assert by_kind["alert"]["title"] == "Alert"
        assert by_kind["alert"]["body"] == "Give pills"
        assert by_kind["main"]["title"] == "Reminder"
        assert by_kind["main"]["body"] == "Give pills"
        assert by_kind["alert"]["collapseId"] != by_kind["main"]["collapseId"]
