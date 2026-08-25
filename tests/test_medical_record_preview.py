"""
test_medical_record_preview.py — List-card reminder fields on medical records.
"""
from bson import ObjectId

from tests.conftest import HEADERS_A, make_pet, make_medical_record, make_reminder


class TestMedicalRecordListPreview:
    def test_list_includes_linked_reminder_from_latest_note(self, client):
        pet = make_pet(client, HEADERS_A)
        record = make_medical_record(client, pet["id"], HEADERS_A, title="Ear infection")
        reminder = make_reminder(
            client,
            pet["id"],
            HEADERS_A,
            title="Give drops",
            date="2099-08-01",
            time="14:30",
        )

        r = client.post(
            f"/api/v1/pets/{pet['id']}/medical-records/{record['id']}/notes",
            json={"text": "Left ear looks better", "linked_reminder_id": reminder["id"]},
            headers=HEADERS_A,
        )
        assert r.status_code == 201, r.text

        listed = client.get(
            f"/api/v1/pets/{pet['id']}/medical-records?status=active",
            headers=HEADERS_A,
        )
        assert listed.status_code == 200
        items = listed.json()
        assert len(items) == 1
        item = items[0]
        assert item["latest_note_preview"] == "Left ear looks better"
        assert item["latest_note_id"] == r.json()["id"]
        assert item["linked_reminder_date"] == "2099-08-01"
        assert item["linked_reminder_time"] == "14:30"

    def test_list_omits_reminder_when_latest_note_has_none(self, client):
        pet = make_pet(client, HEADERS_A)
        record = make_medical_record(client, pet["id"], HEADERS_A, title="Allergy")
        old_reminder = make_reminder(
            client, pet["id"], HEADERS_A, title="Old", date="2099-07-01", time="09:00"
        )

        client.post(
            f"/api/v1/pets/{pet['id']}/medical-records/{record['id']}/notes",
            json={"text": "First note", "linked_reminder_id": old_reminder["id"]},
            headers=HEADERS_A,
        )
        client.post(
            f"/api/v1/pets/{pet['id']}/medical-records/{record['id']}/notes",
            json={"text": "Latest without reminder"},
            headers=HEADERS_A,
        )

        listed = client.get(
            f"/api/v1/pets/{pet['id']}/medical-records?status=active",
            headers=HEADERS_A,
        ).json()[0]

        assert listed["latest_note_preview"] == "Latest without reminder"
        assert listed["linked_reminder_date"] is None
        assert listed["linked_reminder_time"] is None

    def test_list_survives_missing_resolved_at_after_reopen(self, client, mock_db):
        """
        Legacy reopen $unset resolved_at. Listing that active record must not 500 —
        Home/Topics map 5xx to a fake 'check your connection' error.
        """
        pet = make_pet(client, HEADERS_A)
        record = make_medical_record(client, pet["id"], HEADERS_A, title="Skin issue")

        resolved = client.patch(
            f"/api/v1/pets/{pet['id']}/medical-records/{record['id']}/status",
            json={"status": "resolved"},
            headers=HEADERS_A,
        )
        assert resolved.status_code == 200

        reopened = client.patch(
            f"/api/v1/pets/{pet['id']}/medical-records/{record['id']}/status",
            json={"status": "active"},
            headers=HEADERS_A,
        )
        assert reopened.status_code == 200
        assert reopened.json().get("resolved_at") is None

        # Simulate older documents that still have the field fully removed.
        mock_db._db.medical_records.update_one(
            {"_id": ObjectId(record["id"])},
            {"$unset": {"resolved_at": ""}},
        )

        listed = client.get(
            f"/api/v1/pets/{pet['id']}/medical-records?status=active",
            headers=HEADERS_A,
        )
        assert listed.status_code == 200, listed.text
        items = listed.json()
        assert len(items) == 1
        assert items[0]["id"] == record["id"]
        assert items[0]["resolved_at"] is None
