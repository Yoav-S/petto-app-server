"""Free-plan reminder cap and locked extra pets after a downgrade."""
from datetime import datetime, timedelta, timezone

from tests.conftest import HEADERS_A, USER_A_UID, make_pet, make_reminder


def _insert_extra_pet(mock_db, *, name: str = "Max") -> str:
    """Insert a 2nd pet without going through POST /pets (simulates a downgrade)."""
    later = datetime.now(timezone.utc) + timedelta(seconds=2)
    result = mock_db.pets._col.insert_one(
        {
            "name": name,
            "type": "Cat",
            "user_id": USER_A_UID,
            "created_at": later,
            "photo_url": None,
            "breed": None,
            "birth_date": None,
            "sex": None,
            "weight": None,
            "chip_id": None,
            "passport_number": None,
            "color": None,
            "is_neutered": None,
            "notes": None,
        }
    )
    return str(result.inserted_id)


def _grant_premium(mock_db) -> None:
    mock_db.users._col.insert_one(
        {
            "firebase_uid": USER_A_UID,
            "subscription": {
                "plan": "premium",
                "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
                "will_renew": True,
            },
        }
    )


class TestFreePlanPets:
    def test_second_pet_create_blocked(self, client):
        make_pet(client, HEADERS_A)
        r = client.post(
            "/api/v1/pets",
            json={"name": "Max", "type": "Cat"},
            headers=HEADERS_A,
        )
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "premium_required_pet"

    def test_list_marks_extra_pets_locked(self, client, mock_db):
        first = make_pet(client, HEADERS_A)
        extra_id = _insert_extra_pet(mock_db)
        r = client.get("/api/v1/pets", headers=HEADERS_A)
        assert r.status_code == 200
        by_id = {p["id"]: p for p in r.json()}
        assert by_id[first["id"]]["locked"] is False
        assert by_id[extra_id]["locked"] is True

    def test_extra_pet_data_is_blocked(self, client, mock_db):
        first = make_pet(client, HEADERS_A)
        extra_id = _insert_extra_pet(mock_db)

        assert client.get(f"/api/v1/pets/{first['id']}", headers=HEADERS_A).status_code == 200

        blocked = client.get(f"/api/v1/pets/{extra_id}", headers=HEADERS_A)
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["code"] == "premium_required_pet_access"

        reminders = client.get(
            f"/api/v1/pets/{extra_id}/reminders?tab=today", headers=HEADERS_A
        )
        assert reminders.status_code == 403
        assert reminders.json()["detail"]["code"] == "premium_required_pet_access"

        vaccines = client.get(f"/api/v1/pets/{extra_id}/vaccinations", headers=HEADERS_A)
        assert vaccines.status_code == 403

        records = client.get(
            f"/api/v1/pets/{extra_id}/medical-records?status=active",
            headers=HEADERS_A,
        )
        assert records.status_code == 403

    def test_premium_unlocks_extra_pet(self, client, mock_db):
        make_pet(client, HEADERS_A)
        extra_id = _insert_extra_pet(mock_db)
        _grant_premium(mock_db)
        r = client.get(f"/api/v1/pets/{extra_id}", headers=HEADERS_A)
        assert r.status_code == 200
        listed = client.get("/api/v1/pets", headers=HEADERS_A).json()
        assert all(p["locked"] is False for p in listed)


class TestFreePlanReminders:
    def test_active_reminder_cap_is_enforced(self, client, monkeypatch):
        import app.core.subscription as sub

        monkeypatch.setattr(sub, "FREE_MAX_ACTIVE_REMINDERS", 2)
        pet = make_pet(client, HEADERS_A)
        make_reminder(client, pet["id"], HEADERS_A, date="2099-01-01", time="09:00")
        make_reminder(client, pet["id"], HEADERS_A, date="2099-01-02", time="09:00")
        r = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={
                "title": "Over cap",
                "date": "2099-01-03",
                "time": "09:00",
                "repeat": "off",
            },
            headers=HEADERS_A,
        )
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "premium_required_reminder"

    def test_deleting_a_reminder_frees_a_slot(self, client, monkeypatch):
        import app.core.subscription as sub

        monkeypatch.setattr(sub, "FREE_MAX_ACTIVE_REMINDERS", 1)
        pet = make_pet(client, HEADERS_A)
        existing = make_reminder(client, pet["id"], HEADERS_A, date="2099-01-01")
        blocked = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={
                "title": "Over cap",
                "date": "2099-01-02",
                "time": "09:00",
                "repeat": "off",
            },
            headers=HEADERS_A,
        )
        assert blocked.status_code == 403

        deleted = client.delete(
            f"/api/v1/pets/{pet['id']}/reminders/{existing['id']}",
            headers=HEADERS_A,
        )
        assert deleted.status_code == 204

        created = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={
                "title": "Fits now",
                "date": "2099-01-02",
                "time": "09:00",
                "repeat": "off",
            },
            headers=HEADERS_A,
        )
        assert created.status_code == 201
