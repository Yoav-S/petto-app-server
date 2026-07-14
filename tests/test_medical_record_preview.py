"""
test_medical_record_preview.py — List-card reminder fields on medical records.
"""
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
            date="2026-08-01",
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
        assert item["linked_reminder_date"] == "2026-08-01"
        assert item["linked_reminder_time"] == "14:30"

    def test_list_omits_reminder_when_latest_note_has_none(self, client):
        pet = make_pet(client, HEADERS_A)
        record = make_medical_record(client, pet["id"], HEADERS_A, title="Allergy")
        old_reminder = make_reminder(
            client, pet["id"], HEADERS_A, title="Old", date="2026-07-01", time="09:00"
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
