"""
test_ownership.py — Cross-user access and invalid ID tests.

Rule being tested (server-rules §2, §8):
  No cross-user access ever.
  Validate ownership chain: user → pet → entity.
  Return 404 for both "not found" and "not yours" (don't reveal existence).

Every test follows the same pattern:
  - User A creates a resource
  - User B tries to access / modify / delete it
  - Expected: 404 (not 403 — we don't reveal the resource exists)
"""
import pytest
from tests.conftest import HEADERS_A, HEADERS_B, make_pet, make_reminder, make_medical_record


class TestPetOwnership:
    """User B cannot access User A's pets."""

    def test_user_b_cannot_get_user_a_pet(self, client):
        """GET /pets/{id} returns 404 for other user's pet."""
        pet = make_pet(client, HEADERS_A)
        r = client.get(f"/api/v1/pets/{pet['id']}", headers=HEADERS_B)
        assert r.status_code == 404

    def test_user_b_cannot_patch_user_a_pet(self, client):
        """PATCH /pets/{id} returns 404 for other user's pet."""
        pet = make_pet(client, HEADERS_A)
        r = client.patch(
            f"/api/v1/pets/{pet['id']}", json={"name": "Hacked"}, headers=HEADERS_B
        )
        assert r.status_code == 404

    def test_user_b_cannot_delete_user_a_pet(self, client):
        """DELETE /pets/{id} returns 404 for other user's pet."""
        pet = make_pet(client, HEADERS_A)
        r = client.delete(f"/api/v1/pets/{pet['id']}", headers=HEADERS_B)
        assert r.status_code == 404

    def test_invalid_pet_id_returns_404(self, client):
        """Non-ObjectId string as pet_id returns 404."""
        r = client.get("/api/v1/pets/not-a-valid-id", headers=HEADERS_A)
        assert r.status_code == 404


class TestReminderOwnership:
    """User B cannot access User A's reminders even with a valid pet_id."""

    def test_user_b_cannot_list_user_a_reminders(self, client):
        """GET /pets/{id}/reminders returns 404 for other user's pet."""
        pet = make_pet(client, HEADERS_A)
        r = client.get(
            f"/api/v1/pets/{pet['id']}/reminders?tab=today", headers=HEADERS_B
        )
        assert r.status_code == 404

    def test_user_b_cannot_create_reminder_on_user_a_pet(self, client):
        """POST /pets/{id}/reminders returns 404 for other user's pet."""
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/reminders",
            json={"title": "Test", "date": "2099-01-01", "time": "09:00"},
            headers=HEADERS_B,
        )
        assert r.status_code == 404

    def test_user_b_cannot_delete_user_a_reminder(self, client):
        """DELETE /pets/{id}/reminders/{rid} returns 404 for other user."""
        pet = make_pet(client, HEADERS_A)
        reminder = make_reminder(client, pet["id"], HEADERS_A)
        r = client.delete(
            f"/api/v1/pets/{pet['id']}/reminders/{reminder['id']}",
            headers=HEADERS_B,
        )
        assert r.status_code == 404


class TestVaccinationOwnership:
    """User B cannot access User A's vaccinations."""

    def test_user_b_cannot_list_user_a_vaccinations(self, client):
        pet = make_pet(client, HEADERS_A)
        r = client.get(f"/api/v1/pets/{pet['id']}/vaccinations", headers=HEADERS_B)
        assert r.status_code == 404

    def test_user_b_cannot_create_vaccination_on_user_a_pet(self, client):
        pet = make_pet(client, HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/vaccinations",
            json={"name": "Rabies", "date": "2025-01-01"},
            headers=HEADERS_B,
        )
        assert r.status_code == 404


class TestMedicalRecordOwnership:
    """User B cannot access User A's health conditions."""

    def test_user_b_cannot_list_user_a_records(self, client):
        pet = make_pet(client, HEADERS_A)
        r = client.get(
            f"/api/v1/pets/{pet['id']}/medical-records", headers=HEADERS_B
        )
        assert r.status_code == 404

    def test_user_b_cannot_add_note_to_user_a_record(self, client):
        pet = make_pet(client, HEADERS_A)
        record = make_medical_record(client, pet["id"], HEADERS_A)
        r = client.post(
            f"/api/v1/pets/{pet['id']}/medical-records/{record['id']}/notes",
            json={"text": "Injected note"},
            headers=HEADERS_B,
        )
        assert r.status_code == 404


class TestUnauthenticated:
    """All protected routes require a valid Bearer token."""

    def test_no_token_returns_403(self, client):
        """Starlette HTTPBearer returns 403 when no token is provided."""
        r = client.get("/api/v1/pets")
        assert r.status_code == 403

    def test_invalid_token_returns_401(self, client):
        """Unknown token raises 401."""
        r = client.get("/api/v1/pets", headers={"Authorization": "Bearer bad_token"})
        assert r.status_code == 401
