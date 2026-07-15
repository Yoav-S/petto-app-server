"""
test_medical_record_description.py — Health record description on create + list.
"""
from tests.conftest import HEADERS_A, make_pet


class TestMedicalRecordDescription:
    def test_create_stores_and_list_returns_description_and_created_at(self, client):
        pet = make_pet(client, HEADERS_A)
        payload = {
            "title": "Skin allergy",
            "description": "Red spots on belly, started last week",
        }
        r = client.post(
            f"/api/v1/pets/{pet['id']}/medical-records",
            json=payload,
            headers=HEADERS_A,
        )
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["title"] == payload["title"]
        assert created["description"] == payload["description"]
        assert created["created_at"] is not None

        listed = client.get(
            f"/api/v1/pets/{pet['id']}/medical-records?status=active",
            headers=HEADERS_A,
        )
        assert listed.status_code == 200
        item = listed.json()[0]
        assert item["description"] == payload["description"]
        assert item["created_at"] is not None

    def test_create_without_description_returns_null_description(self, client):
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/medical-records",
            json={"title": "Checkup"},
            headers=HEADERS_A,
        )
        assert r.status_code == 201, r.text
        assert r.json()["description"] is None
