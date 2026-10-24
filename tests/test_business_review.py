"""Onboarding → first branch → review → published, plus the admin one-form."""

import asyncio
from unittest.mock import patch

from bson import ObjectId

from app.core.config import settings
from tests.conftest import HEADERS_A, HEADERS_B, USER_A_UID

HOURS = {
    "provided": True,
    "always_open": False,
    "mon": [{"open": "09:00", "close": "17:00"}],
    "tue": [{"open": "09:00", "close": "17:00"}],
    "wed": [{"open": "09:00", "close": "13:00"}, {"open": "14:00", "close": "18:00"}],
    "thu": [{"open": "09:00", "close": "17:00"}],
    "fri": [{"open": "09:00", "close": "17:00"}],
    "sat": [{"open": "09:00", "close": "13:00"}],
    "sun": [],
}

ADMIN = patch.object(settings, "RAGLY_ADMIN_EMAILS", "uid_user_b@test.com")


def _details(**overrides):
    body = {
        "name": "VetAsist Clinica Veterinară",
        "phone": ["+373 22 221 303", "(022) 78-03-02"],
        "email": "hello@vetasist.md",
        "description": "Veterinary clinic in the centre.",
        "website": "https://vetasist.com.md/",
        "instagram": "@vetasist",
        "locale": "ro",
    }
    body.update(overrides)
    return body


def _branch(**overrides):
    body = {
        "address": "Vasile Lupu 59",
        "city": "Chișinău",
        "location": {"type": "Point", "coordinates": [28.8004, 47.0217]},
        "place_ref": {"provider": "osm", "id": "N123"},
        "services": [
            {"category": "veterinarian", "opening_hours": HOURS, "phone": []},
            {"category": "pharmacy", "opening_hours": {"provided": True, "always_open": True}, "phone": ["+373 60 000 000"]},
        ],
        "phone": [],
    }
    body.update(overrides)
    return body


def _admin_create(**overrides):
    body = {"business": _details(), "branch": _branch(), "owner_email": None}
    body.update(overrides)
    return body


def _mails():
    return (
        patch("app.routers.businesses.send_business_review_email"),
        patch("app.business_mail.send_business_owner_email"),
    )


def test_onboarding_creates_a_draft_with_an_owner_seat(client, mock_db):
    created = client.post("/api/v1/businesses", json=_details(), headers=HEADERS_A)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "draft"
    assert body["locations"] == []
    assert body["owned"] is True
    assert body["locale"] == "ro"
    assert body["website"] == "https://vetasist.com.md/"
    assert body["category"] is None

    member = asyncio.run(mock_db.business_members.find_one({"business_id": body["id"], "user_id": USER_A_UID}))
    assert member["role"] == "owner"

    mine = client.get("/api/v1/businesses/mine", headers=HEADERS_A)
    assert mine.json()["business"]["id"] == body["id"]
    assert mine.json()["role"] == "owner"
    assert mine.json()["locale"] == "ro"

    hidden = client.get("/api/v1/businesses/nearby?anywhere=true", headers=HEADERS_B)
    assert hidden.json()["items"] == []

    second = client.post("/api/v1/businesses", json=_details(name="Second Clinic"), headers=HEADERS_A)
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "business_exists"


def test_onboarding_allows_an_empty_phone(client):
    created = client.post("/api/v1/businesses", json=_details(name="Quiet Clinic", phone=[]), headers=HEADERS_A)
    assert created.status_code == 201, created.text
    assert created.json()["phone"] == []


def test_name_check_and_taken_names(client, mock_db):
    free = client.get("/api/v1/businesses/name-check", params={"name": "Zoomama"}, headers=HEADERS_A)
    assert free.json()["available"] is True
    assert free.json()["claimable"] is False
    first = client.post("/api/v1/businesses", json=_details(name="Zoomama"), headers=HEADERS_A)
    assert first.status_code == 201, first.text
    taken = client.get("/api/v1/businesses/name-check", params={"name": "  zoomama "}, headers=HEADERS_B)
    assert taken.json()["available"] is False
    assert taken.json()["claimable"] is False
    same = client.get(
        "/api/v1/businesses/name-check",
        params={"name": "ZOOMAMA", "business_id": first.json()["id"]},
        headers=HEADERS_A,
    )
    assert same.json()["available"] is True
    again = client.post("/api/v1/businesses", json=_details(name="ZOOMAMA"), headers=HEADERS_B)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "business_name_taken"
    assert again.json()["detail"]["claimable"] is False

    asyncio.run(mock_db.businesses.insert_one({"name": "City Vet", "name_key": "city vet"}))
    unowned = client.get("/api/v1/businesses/name-check", params={"name": "City Vet"}, headers=HEADERS_A)
    assert unowned.json()["available"] is False
    assert unowned.json()["claimable"] is True
    claim = client.post("/api/v1/businesses", json=_details(name="City Vet"), headers=HEADERS_B)
    assert claim.status_code == 409
    assert claim.json()["detail"]["code"] == "business_name_taken"
    assert claim.json()["detail"]["claimable"] is True

    invited = asyncio.run(mock_db.businesses.insert_one({"name": "Invited Clinic", "name_key": "invited clinic"}))
    asyncio.run(
        mock_db.business_invitations.insert_one(
            {"business_id": str(invited.inserted_id), "role": "owner", "status": "pending"}
        )
    )
    held = client.get("/api/v1/businesses/name-check", params={"name": "Invited Clinic"}, headers=HEADERS_A)
    assert held.json()["available"] is False
    assert held.json()["claimable"] is False


def test_first_branch_from_draft_to_published(client, mock_db):
    admins, owner_mail = _mails()
    with admins as admin_mail, owner_mail as owner_mails, ADMIN:
        business = client.post("/api/v1/businesses", json=_details(), headers=HEADERS_A).json()
        business_id = business["id"]

        empty = client.post(f"/api/v1/businesses/{business_id}/locations", json={}, headers=HEADERS_A)
        assert empty.status_code == 201, empty.text
        assert empty.json()["status"] == "draft"
        branch_id = empty.json()["id"]

        incomplete = client.post(f"/api/v1/businesses/{business_id}/locations/{branch_id}/send", headers=HEADERS_A)
        assert incomplete.status_code == 400
        assert incomplete.json()["detail"]["code"] == "branch_incomplete"
        assert set(incomplete.json()["detail"]["fields"]) == {"address", "location", "services"}

        saved = client.patch(f"/api/v1/businesses/{business_id}/locations/{branch_id}", json=_branch(), headers=HEADERS_A)
        assert saved.status_code == 200, saved.text
        assert saved.json()["is_main"] is True
        assert saved.json()["timezone"] == "Europe/Chisinau"
        assert [s["category"] for s in saved.json()["services"]] == ["veterinarian", "pharmacy"]
        assert saved.json()["services"][1]["opening_hours"]["always_open"] is True
        assert saved.json()["services"][1]["opening_hours"]["mon"] == [{"open": "00:00", "close": "24:00"}]
        assert saved.json()["opening_hours"]["wed"] == HOURS["wed"]

        still_draft = client.get("/api/v1/businesses/mine", headers=HEADERS_A).json()["business"]
        assert still_draft["status"] == "draft"
        assert still_draft["category"] == "veterinarian"
        assert still_draft["categories"] == ["veterinarian", "pharmacy"]
        assert still_draft["address"] == "Vasile Lupu 59"
        assert admin_mail.call_count == 0

        sent = client.post(f"/api/v1/businesses/{business_id}/locations/{branch_id}/send", headers=HEADERS_A)
        assert sent.status_code == 200, sent.text
        assert sent.json()["status"] == "pending_review"
        assert sent.json()["submitted_at"]
        assert admin_mail.call_count == 1
        assert owner_mails.call_args.kwargs["subject"] == "Ragly: VetAsist Clinica Veterinară așteaptă verificarea"
        assert owner_mails.call_args.args[0] == "uid_user_a@test.com"

        waiting = client.get("/api/v1/businesses/mine", headers=HEADERS_A).json()["business"]
        assert waiting["status"] == "pending_review"
        assert waiting["locations"][0]["status"] == "pending_review"

        forbidden = client.get("/api/v1/admin/review", headers=HEADERS_A)
        assert forbidden.status_code == 403
        queue = client.get("/api/v1/admin/review", headers=HEADERS_B)
        assert queue.status_code == 200, queue.text
        assert len(queue.json()) == 1
        group = queue.json()[0]
        assert group["first_request"] is True
        assert group["owner_email"] == "uid_user_a@test.com"
        assert group["business"]["id"] == business_id
        assert group["branches"][0]["branch"]["id"] == branch_id
        assert group["branches"][0]["conflicts"] == []
        summary = client.get("/api/v1/admin/businesses/summary", headers=HEADERS_B).json()
        assert summary["pending"] == 1

        approved = client.post(
            f"/api/v1/admin/locations/{branch_id}/approve",
            json={"field_notes": {"website": "Add the booking page."}},
            headers=HEADERS_B,
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "published"
        assert approved.json()["admin_notes"] == {"website": "Add the booking page."}
        assert all(s["status"] == "published" for s in approved.json()["services"])
        assert owner_mails.call_args.kwargs["subject"] == "Ragly: VetAsist Clinica Veterinară este activ"
        assert owner_mails.call_args.kwargs["lines"] == ["Site web: Add the booking page."]

        live = client.get("/api/v1/businesses/mine", headers=HEADERS_A).json()
        assert live["business"]["status"] == "published"
        assert live["business"]["admin_notes"] == {"website": "Add the booking page."}
        assert live["role"] == "owner"
        assert [n["kind"] for n in live["notices"]] == ["business_published"]

        seen = client.post("/api/v1/businesses/seen", headers=HEADERS_A)
        assert seen.status_code == 204
        assert client.get("/api/v1/businesses/mine", headers=HEADERS_A).json()["notices"] == []

        assert client.get("/api/v1/admin/review", headers=HEADERS_B).json() == []

    nearby = client.get("/api/v1/businesses/nearby?latitude=47.02&longitude=28.80", headers=HEADERS_B)
    assert nearby.status_code == 200, nearby.text
    card = nearby.json()["items"][0]
    assert card["name"] == "VetAsist Clinica Veterinară"
    assert card["category"] == "veterinarian"
    assert card["categories"] == ["veterinarian", "pharmacy"]
    assert card["location_count"] == 1
    assert card["address"] == "Vasile Lupu 59"

    pharmacy = client.get("/api/v1/businesses/nearby?latitude=47.02&longitude=28.80&category=pharmacy", headers=HEADERS_B)
    assert pharmacy.json()["items"][0]["category"] == "pharmacy"
    assert pharmacy.json()["items"][0]["open_24_7"] is True
    groomer = client.get("/api/v1/businesses/nearby?latitude=47.02&longitude=28.80&category=groomer", headers=HEADERS_B)
    assert groomer.json()["items"] == []

    detail = client.get(f"/api/v1/businesses/{business_id}?latitude=47.02&longitude=28.80", headers=HEADERS_B)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["phone"] == ["+373 22 221 303", "(022) 78-03-02"]
    assert body["opening_hours"]["wed"] == HOURS["wed"]
    assert len(body["locations"]) == 1
    branch = body["locations"][0]
    assert branch["call_phone"] == ["+373 22 221 303", "(022) 78-03-02"]
    assert branch["shared_phone"] is True
    assert branch["categories"] == ["veterinarian", "pharmacy"]
    assert branch["services"][1]["phone"] == ["+373 60 000 000"]


def test_reject_fix_and_resend(client, mock_db):
    admins, owner_mail = _mails()
    with admins, owner_mail as owner_mails, ADMIN:
        business_id = client.post("/api/v1/businesses", json=_details(locale="ru"), headers=HEADERS_A).json()["id"]
        branch_id = client.post(f"/api/v1/businesses/{business_id}/locations", json=_branch(), headers=HEADERS_A).json()["id"]
        client.post(f"/api/v1/businesses/{business_id}/locations/{branch_id}/send", headers=HEADERS_A)

        no_notes = client.post(f"/api/v1/admin/locations/{branch_id}/reject", json={"field_errors": {}}, headers=HEADERS_B)
        assert no_notes.status_code == 400

        rejected = client.post(
            f"/api/v1/admin/locations/{branch_id}/reject",
            json={"field_errors": {"address": "Add the building number.", "hours": "Saturday looks wrong."}},
            headers=HEADERS_B,
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "rejected"
        assert rejected.json()["field_errors"]["address"] == "Add the building number."
        assert owner_mails.call_args.kwargs["subject"] == "Ragly: VetAsist Clinica Veterinară требует изменений"
        assert owner_mails.call_args.kwargs["lines"] == ["Адрес: Add the building number.", "Часы работы: Saturday looks wrong."]

        mine = client.get("/api/v1/businesses/mine", headers=HEADERS_A).json()
        assert mine["business"]["status"] == "rejected"
        assert mine["business"]["field_errors"]["address"] == "Add the building number."
        assert mine["notices"][0]["kind"] == "branch_rejected"
        summary = client.get("/api/v1/admin/businesses/summary", headers=HEADERS_B).json()
        assert summary["pending"] == 0 and summary["rejected"] == 1

        twice = client.post(f"/api/v1/admin/locations/{branch_id}/reject", json={"reason": "again"}, headers=HEADERS_B)
        assert twice.status_code == 400
        assert twice.json()["detail"]["code"] == "branch_not_pending"

        fixed = client.patch(
            f"/api/v1/businesses/{business_id}/locations/{branch_id}",
            json=_branch(address="Vasile Lupu 59/1"),
            headers=HEADERS_A,
        )
        assert fixed.status_code == 200, fixed.text
        assert fixed.json()["status"] == "rejected"
        assert fixed.json()["field_errors"]["address"] == "Add the building number."

        resent = client.post(f"/api/v1/businesses/{business_id}/locations/{branch_id}/send", headers=HEADERS_A)
        assert resent.status_code == 200, resent.text
        assert resent.json()["status"] == "pending_review"
        assert resent.json()["field_errors"] == {}

        withdrawn = client.post(f"/api/v1/businesses/{business_id}/locations/{branch_id}/withdraw", headers=HEADERS_A)
        assert withdrawn.json()["status"] == "draft"
        assert client.get("/api/v1/businesses/mine", headers=HEADERS_A).json()["business"]["status"] == "draft"
        client.post(f"/api/v1/businesses/{business_id}/locations/{branch_id}/send", headers=HEADERS_A)

        approved = client.post(f"/api/v1/admin/businesses/{business_id}/approve", headers=HEADERS_B)
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "published"
        assert approved.json()["field_errors"] == {}
        assert approved.json()["locations"][0]["status"] == "published"
        assert approved.json()["address"] == "Vasile Lupu 59/1"


def test_live_branch_keeps_its_address_and_new_service_waits(client, mock_db):
    admins, owner_mail = _mails()
    with admins as admin_mail, owner_mail, ADMIN:
        business_id = client.post("/api/v1/businesses", json=_details(), headers=HEADERS_A).json()["id"]
        branch_id = client.post(f"/api/v1/businesses/{business_id}/locations", json=_branch(), headers=HEADERS_A).json()["id"]
        client.post(f"/api/v1/businesses/{business_id}/locations/{branch_id}/send", headers=HEADERS_A)
        client.post(f"/api/v1/admin/locations/{branch_id}/approve", headers=HEADERS_B)
        admin_mail.reset_mock()

        moved = client.patch(
            f"/api/v1/businesses/{business_id}/locations/{branch_id}",
            json=_branch(address="Somewhere else 1"),
            headers=HEADERS_A,
        )
        assert moved.status_code == 409
        assert moved.json()["detail"]["code"] == "branch_not_editable"

        extended = client.patch(
            f"/api/v1/businesses/{business_id}/locations/{branch_id}",
            json=_branch(
                phone=["+373 69 000 000"],
                services=[
                    *_branch()["services"],
                    {"category": "groomer", "opening_hours": HOURS, "phone": []},
                ],
            ),
            headers=HEADERS_A,
        )
        assert extended.status_code == 200, extended.text
        assert extended.json()["status"] == "published"
        by_category = {s["category"]: s for s in extended.json()["services"]}
        assert by_category["veterinarian"]["status"] == "published"
        assert by_category["groomer"]["status"] == "pending_review"
        assert admin_mail.call_count == 1

        still_live = client.get("/api/v1/businesses/nearby?anywhere=true", headers=HEADERS_B).json()["items"][0]
        assert still_live["categories"] == ["veterinarian", "pharmacy"]
        no_groomer = client.get("/api/v1/businesses/nearby?anywhere=true&category=groomer", headers=HEADERS_B)
        assert no_groomer.json()["items"] == []

        queue = client.get("/api/v1/admin/review", headers=HEADERS_B).json()
        assert len(queue) == 1
        assert queue[0]["first_request"] is False
        assert client.get("/api/v1/admin/businesses/summary", headers=HEADERS_B).json()["pending"] == 1

        approved = client.post(f"/api/v1/admin/locations/{branch_id}/approve", headers=HEADERS_B)
        assert {s["status"] for s in approved.json()["services"]} == {"published"}
        groomer = client.get("/api/v1/businesses/nearby?anywhere=true&category=groomer", headers=HEADERS_B)
        assert groomer.json()["items"][0]["category"] == "groomer"

        paused = client.patch(
            f"/api/v1/businesses/{business_id}/locations/{branch_id}/visibility",
            json={"visible": False, "reason": "Renovation"},
            headers=HEADERS_A,
        )
        assert paused.json()["visible"] is False
        assert client.get("/api/v1/businesses/nearby?anywhere=true", headers=HEADERS_B).json()["items"] == []
        resumed = client.patch(
            f"/api/v1/businesses/{business_id}/locations/{branch_id}/visibility",
            json={"visible": True},
            headers=HEADERS_A,
        )
        assert resumed.json()["visible"] is True
        assert len(client.get("/api/v1/businesses/nearby?anywhere=true", headers=HEADERS_B).json()["items"]) == 1

        hidden = client.patch(f"/api/v1/businesses/{business_id}/visibility", json={"visible": False}, headers=HEADERS_A)
        assert hidden.status_code == 200, hidden.text
        assert hidden.json()["visible"] is False
        assert hidden.json()["status"] == "published"
        assert client.get("/api/v1/businesses/nearby?anywhere=true", headers=HEADERS_B).json()["items"] == []
        assert client.get(f"/api/v1/businesses/{business_id}", headers=HEADERS_B).status_code == 404
        shown = client.patch(f"/api/v1/businesses/{business_id}/visibility", json={"visible": True}, headers=HEADERS_B)
        assert shown.json()["visible"] is True
        assert len(client.get("/api/v1/businesses/nearby?anywhere=true", headers=HEADERS_B).json()["items"]) == 1


def test_bad_hours_and_contacts_are_refused(client):
    business_id = client.post("/api/v1/businesses", json=_details(), headers=HEADERS_A).json()["id"]
    backwards = client.post(
        f"/api/v1/businesses/{business_id}/locations",
        json=_branch(services=[{"category": "groomer", "opening_hours": {**HOURS, "mon": [{"open": "18:00", "close": "09:00"}]}}]),
        headers=HEADERS_A,
    )
    assert backwards.status_code == 400
    assert backwards.json()["detail"]["code"] == "branch_hours_invalid"
    overlapping = client.post(
        f"/api/v1/businesses/{business_id}/locations",
        json=_branch(services=[{"category": "groomer", "opening_hours": {**HOURS, "mon": [{"open": "09:00", "close": "13:00"}, {"open": "12:00", "close": "18:00"}]}}]),
        headers=HEADERS_A,
    )
    assert overlapping.status_code == 400
    bad_phone = client.post("/api/v1/businesses", json=_details(name="Other", phone=["call me"]), headers=HEADERS_A)
    assert bad_phone.status_code == 422
    bad_site = client.post("/api/v1/businesses", json=_details(name="Other", website="javascript:alert(1)"), headers=HEADERS_A)
    assert bad_site.status_code == 422
    plain = client.post(
        "/api/v1/businesses",
        json=_details(name="Plain", website="vetasist.com.md", instagram="https://instagram.com/zoomama", email=None, description=None),
        headers=HEADERS_B,
    )
    assert plain.status_code == 201, plain.text
    assert plain.json()["website"] == "https://vetasist.com.md"
    assert plain.json()["instagram"] == "@zoomama"
    assert plain.json()["email"] is None


def test_special_dates_and_no_fixed_hours(client, mock_db):
    with ADMIN:
        business_id = client.post("/api/v1/businesses", json=_details(), headers=HEADERS_A).json()["id"]
        hours = {**HOURS, "special_dates": [{"date": "2026-12-25", "closed": True}, {"date": "2026-12-31", "slots": [{"open": "09:00", "close": "13:00"}]}]}
        saved = client.post(
            f"/api/v1/businesses/{business_id}/locations",
            json=_branch(services=[
                {"category": "veterinarian", "opening_hours": hours},
                {"category": "pet_friendly", "opening_hours": {"provided": False}},
            ]),
            headers=HEADERS_A,
        )
        assert saved.status_code == 201, saved.text
        services = saved.json()["services"]
        assert services[0]["opening_hours"]["special_dates"][0] == {"date": "2026-12-25", "closed": True, "slots": []}
        assert services[1]["opening_hours"]["provided"] is False
        assert services[1]["opening_hours"]["mon"] == []
        duplicate = client.post(
            f"/api/v1/businesses/{business_id}/locations",
            json=_branch(services=[{"category": "veterinarian", "opening_hours": {**hours, "special_dates": hours["special_dates"] + [{"date": "2026-12-25", "closed": True}]}}]),
            headers=HEADERS_A,
        )
        assert duplicate.status_code == 400


def test_admin_one_form_writes_the_same_documents_as_the_owner_flow(client, mock_db):
    admins, owner_mail = _mails()
    with admins, owner_mail, ADMIN:
        owner_business = client.post("/api/v1/businesses", json=_details(), headers=HEADERS_A).json()
        branch_id = client.post(f"/api/v1/businesses/{owner_business['id']}/locations", json=_branch(), headers=HEADERS_A).json()["id"]
        client.post(f"/api/v1/businesses/{owner_business['id']}/locations/{branch_id}/send", headers=HEADERS_A)
        client.post(f"/api/v1/admin/locations/{branch_id}/approve", headers=HEADERS_B)

        created = client.post(
            "/api/v1/admin/businesses",
            json=_admin_create(business=_details(name="Admin Clinic", locale="en"), owner_email="clinic@example.com"),
            headers=HEADERS_B,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["status"] == "published"
        assert body["owned"] is False
        assert body["category"] == "veterinarian"
        assert body["categories"] == ["veterinarian", "pharmacy"]
        assert body["invitations"][0]["email"] == "clinic@example.com"
        assert body["locations"][0]["status"] == "published"
        assert body["locations"][0]["is_main"] is True

        denied = client.post("/api/v1/admin/businesses", json=_admin_create(), headers=HEADERS_A)
        assert denied.status_code == 403
        incomplete = client.post(
            "/api/v1/admin/businesses",
            json=_admin_create(business=_details(name="No Pin"), branch=_branch(location=None)),
            headers=HEADERS_B,
        )
        assert incomplete.status_code == 400
        assert incomplete.json()["detail"]["fields"] == ["location"]

    owner_doc = asyncio.run(mock_db.businesses.find_one({"_id": ObjectId(owner_business["id"])}))
    admin_doc = asyncio.run(mock_db.businesses.find_one({"_id": ObjectId(body["id"])}))
    assert set(owner_doc) == set(admin_doc), set(owner_doc) ^ set(admin_doc)
    for key in ("name_key", "categories", "category", "status", "opening_hours", "location", "timezone", "photos", "locale"):
        assert type(owner_doc[key]) is type(admin_doc[key]), key
    owner_branch = asyncio.run(mock_db.business_locations.find_one({"business_id": owner_business["id"]}))
    admin_branch = asyncio.run(mock_db.business_locations.find_one({"business_id": body["id"]}))
    assert set(owner_branch) == set(admin_branch), set(owner_branch) ^ set(admin_branch)
    assert set(owner_branch["services"][0]) == set(admin_branch["services"][0])
    assert owner_branch["source"] == "owner" and admin_branch["source"] == "admin"

    nearby = client.get("/api/v1/businesses/nearby?anywhere=true", headers=HEADERS_A).json()["items"]
    assert [item["name"] for item in nearby] == ["Admin Clinic", "VetAsist Clinica Veterinară"]


def test_admin_edits_details_and_branches_without_review(client, mock_db):
    with ADMIN:
        created = client.post("/api/v1/admin/businesses", json=_admin_create(), headers=HEADERS_B).json()
        business_id = created["id"]
        branch_id = created["locations"][0]["id"]
        edited = client.patch(
            f"/api/v1/admin/businesses/{business_id}",
            json=_details(name="VetAsist Updated", email="vetasist@example.com"),
            headers=HEADERS_B,
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["name"] == "VetAsist Updated"
        assert edited.json()["email"] == "vetasist@example.com"
        assert edited.json()["status"] == "published"
        assert edited.json()["address"] == "Vasile Lupu 59"

        moved = client.patch(
            f"/api/v1/businesses/{business_id}/locations/{branch_id}",
            json=_branch(address="Moved 2", services=[*_branch()["services"], {"category": "groomer", "opening_hours": HOURS}]),
            headers=HEADERS_B,
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["address"] == "Moved 2"
        assert {s["status"] for s in moved.json()["services"]} == {"published"}
        assert client.get("/api/v1/admin/review", headers=HEADERS_B).json() == []

        second = client.post(
            f"/api/v1/businesses/{business_id}/locations",
            json=_branch(address="Second 5", location={"type": "Point", "coordinates": [28.81, 47.03]}),
            headers=HEADERS_B,
        )
        assert second.status_code == 201, second.text
        assert second.json()["status"] == "published"
        assert second.json()["is_main"] is False
        cards = client.get("/api/v1/businesses/nearby?anywhere=true", headers=HEADERS_A).json()["items"]
        assert sorted(item["address"] for item in cards) == ["Moved 2", "Second 5"]

        gone = client.delete(f"/api/v1/businesses/{business_id}/locations/{branch_id}", headers=HEADERS_B)
        assert gone.status_code == 204
        remaining = client.get("/api/v1/businesses/mine", headers=HEADERS_B)
        listed = client.get("/api/v1/admin/businesses", headers=HEADERS_B).json()[0]
        assert len(listed["locations"]) == 1
        assert listed["locations"][0]["is_main"] is True
        assert listed["address"] == "Second 5"
        del remaining


def test_duplicate_place_is_flagged_for_the_admin(client):
    admins, owner_mail = _mails()
    with admins, owner_mail, ADMIN:
        client.post("/api/v1/admin/businesses", json=_admin_create(), headers=HEADERS_B)
        business_id = client.post("/api/v1/businesses", json=_details(name="Copycat"), headers=HEADERS_A).json()["id"]
        branch_id = client.post(f"/api/v1/businesses/{business_id}/locations", json=_branch(), headers=HEADERS_A).json()["id"]
        client.post(f"/api/v1/businesses/{business_id}/locations/{branch_id}/send", headers=HEADERS_A)
        queue = client.get("/api/v1/admin/review", headers=HEADERS_B).json()
        conflict = queue[0]["branches"][0]["conflicts"][0]
        assert conflict["business_name"] == "VetAsist Clinica Veterinară"
        assert conflict["same_place_ref"] is True
        assert conflict["distance_m"] == 0


def test_owner_invite_is_membership_only_after_approval(client):
    with ADMIN:
        created = client.post(
            "/api/v1/admin/businesses",
            json=_admin_create(owner_email="uid_user_a@test.com"),
            headers=HEADERS_B,
        )
        assert created.status_code == 201, created.text
        business_id = created.json()["id"]
        invitation_id = created.json()["invitations"][0]["id"]

        waiting = client.get("/api/v1/businesses/mine", headers=HEADERS_A)
        assert waiting.json()["business"] is None
        assert waiting.json()["invitations"][0]["status"] == "pending"

        denied = client.post(f"/api/v1/businesses/invitations/{invitation_id}/approve", headers=HEADERS_B)
        assert denied.status_code == 403

        approved = client.post(f"/api/v1/businesses/invitations/{invitation_id}/approve", headers=HEADERS_A)
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        mine = client.get("/api/v1/businesses/mine", headers=HEADERS_A)
        assert mine.json()["role"] == "owner"
        assert mine.json()["business"]["owned"] is True
        assert mine.json()["business"]["locations"][0]["status"] == "published"

        own_edit = client.patch(f"/api/v1/businesses/{business_id}", json=_details(description="Now ours"), headers=HEADERS_A)
        assert own_edit.status_code == 200, own_edit.text
        assert own_edit.json()["description"] == "Now ours"

        worker = client.post(
            f"/api/v1/businesses/{business_id}/invitations",
            json={"email": "worker@example.com", "role": "worker"},
            headers=HEADERS_A,
        )
        assert worker.status_code == 201, worker.text

        removed = client.delete(f"/api/v1/businesses/{business_id}", headers=HEADERS_A)
        assert removed.status_code == 204
        gone = client.get("/api/v1/admin/businesses", headers=HEADERS_B)
        assert all(item["id"] != business_id for item in gone.json())


def test_worker_cannot_delete_business(client, mock_db):
    with ADMIN:
        created = client.post("/api/v1/admin/businesses", json=_admin_create(owner_email="uid_user_a@test.com"), headers=HEADERS_B)
        business_id = created.json()["id"]
        invitation_id = created.json()["invitations"][0]["id"]
        client.post(f"/api/v1/businesses/invitations/{invitation_id}/approve", headers=HEADERS_A)
        asyncio.run(
            mock_db.business_members.update_one(
                {"business_id": business_id, "user_id": "uid_user_a"},
                {"$set": {"role": "worker"}},
            )
        )
        asyncio.run(mock_db.businesses.update_one({"_id": ObjectId(business_id)}, {"$set": {"owner_uid": None}}))
        denied = client.delete(f"/api/v1/businesses/{business_id}", headers=HEADERS_A)
        assert denied.status_code == 403
        removed = client.delete(f"/api/v1/admin/businesses/{business_id}", headers=HEADERS_B)
        assert removed.status_code == 204


def test_logo_gallery_and_branch_photo(client):
    image = ("clinic.png", b"\x89PNG\r\n\x1a\nfake", "image/png")
    urls = [
        f"https://firebasestorage.googleapis.com/v0/b/bucket/o/businesses%2F1%2F{name}.png?alt=media&token=x"
        for name in ("logo", "a", "b", "branch")
    ]
    with ADMIN, patch("app.routers.businesses.upload_business_image", side_effect=urls) as upload, patch(
        "app.routers.businesses.delete_business_image"
    ):
        business_id = client.post("/api/v1/businesses", json=_details(), headers=HEADERS_A).json()["id"]
        logo = client.post(f"/api/v1/businesses/{business_id}/logo", files={"file": image}, headers=HEADERS_A)
        assert logo.status_code == 200, logo.text
        assert logo.json()["photo"] == urls[0]
        assert logo.json()["photos"] == [urls[0]]
        assert upload.call_args.args[2] == "image/png"

        denied = client.post(f"/api/v1/businesses/{business_id}/photos", files={"file": image}, headers=HEADERS_B)
        assert denied.status_code == 403
        added = client.post(f"/api/v1/businesses/{business_id}/photos", files={"file": image}, headers=HEADERS_A)
        assert added.json()["photos"] == [urls[0], urls[1]]
        admin_added = client.post(f"/api/v1/admin/businesses/{business_id}/photos", files={"file": image}, headers=HEADERS_B)
        assert admin_added.json()["photos"] == [urls[0], urls[1], urls[2]]
        removed = client.request("DELETE", f"/api/v1/businesses/{business_id}/photos", json={"url": urls[1]}, headers=HEADERS_A)
        assert removed.json()["photos"] == [urls[0], urls[2]]

        branch_id = client.post(f"/api/v1/businesses/{business_id}/locations", json=_branch(), headers=HEADERS_A).json()["id"]
        photo = client.post(f"/api/v1/businesses/{business_id}/locations/{branch_id}/photo", files={"file": image}, headers=HEADERS_A)
        assert photo.status_code == 200, photo.text
        assert photo.json()["photo"] == urls[3]
        cleared = client.delete(f"/api/v1/businesses/{business_id}/locations/{branch_id}/photo", headers=HEADERS_A)
        assert cleared.json()["photo"] is None
        no_logo = client.delete(f"/api/v1/businesses/{business_id}/logo", headers=HEADERS_A)
        assert no_logo.json()["photo"] is None


def test_summary_counts_and_pages(client):
    with ADMIN:
        created = client.post("/api/v1/admin/businesses", json=_admin_create(owner_email="uid_user_a@test.com"), headers=HEADERS_B)
        business_id = created.json()["id"]
        summary = client.get("/api/v1/admin/businesses/summary", headers=HEADERS_B)
        assert summary.status_code == 200, summary.text
        assert summary.json()["published"] == 1
        assert summary.json()["deleted"] == 0

        page = client.get("/api/v1/admin/businesses/page?limit=1&status=published&category=pharmacy", headers=HEADERS_B)
        assert page.status_code == 200, page.text
        assert len(page.json()["items"]) == 1
        assert page.json()["items"][0]["id"] == business_id
        assert page.json()["has_more"] is False

        removed = client.delete(f"/api/v1/admin/businesses/{business_id}", headers=HEADERS_B)
        assert removed.status_code == 204
        after = client.get("/api/v1/admin/businesses/summary", headers=HEADERS_B)
        assert after.json()["published"] == 0
        assert after.json()["deleted"] == 1


# ---------------------------------------------------------------------------
# Pet-owner endpoints on documents the admins inserted by hand earlier
# ---------------------------------------------------------------------------


def _legacy(name: str, **overrides) -> dict:
    doc = {
        "_id": ObjectId(),
        "name": name,
        "phone": ["+373 69 123 456", "+373 22 000 111"],
        "description": "A modern veterinary clinic.",
        "category": "veterinarian",
        "city": "Chișinău",
        "status": "published",
        "address": "Str. București 45",
        "timezone": "Europe/Chisinau",
        "opening_hours": {"mon": [{"open": "09:00", "close": "19:00"}], "tue": [], "wed": [], "thu": [], "fri": [], "sat": [], "sun": []},
        "website": "https://zoomama.md",
        "instagram": "@zoomama",
        "location": {"type": "Point", "coordinates": [28.83, 47.02]},
    }
    doc.update(overrides)
    return doc


def test_manual_clinic_document_lists_and_updates(client, mock_db):
    doc = _legacy("Vetgor", phone="+373 606 97 607", photo="vetgor.png", website=None, email=None)
    asyncio.run(mock_db.businesses.insert_one(doc))
    with ADMIN:
        listed = client.get("/api/v1/admin/businesses", headers=HEADERS_B)
        assert listed.status_code == 200, listed.text
        row = listed.json()[0]
        assert row["name"] == "Vetgor"
        assert row["phone"] == ["+373 606 97 607"]
        assert row["photo"] == "vetgor.png"
        assert row["owned"] is False
        assert row["location"]["coordinates"][1] == 47.02
        assert row["locations"][0]["services"][0]["category"] == "veterinarian"
        assert row["locations"][0]["is_main"] is True

        updated = client.patch(
            f"/api/v1/admin/businesses/{doc['_id']}",
            json=_details(name="Vetgor Updated", email="vetgor@example.com"),
            headers=HEADERS_B,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["name"] == "Vetgor Updated"
        assert updated.json()["email"] == "vetgor@example.com"
        assert updated.json()["status"] == "published"
        assert updated.json()["address"] == "Str. București 45"


def test_nearby_lists_published_businesses_closest_first(client, mock_db):
    with ADMIN:
        near = client.post(
            "/api/v1/admin/businesses",
            json=_admin_create(business=_details(name="Near Clinic"), branch=_branch(location={"type": "Point", "coordinates": [28.83, 47.02]})),
            headers=HEADERS_B,
        )
        far = client.post(
            "/api/v1/admin/businesses",
            json=_admin_create(business=_details(name="Far Clinic"), branch=_branch(location={"type": "Point", "coordinates": [28.95, 47.20]})),
            headers=HEADERS_B,
        )
        assert near.status_code == 201, near.text
        assert far.status_code == 201, far.text
        asyncio.run(mock_db.business_reviews.insert_one({"business_id": near.json()["id"], "rating": 5}))
        asyncio.run(mock_db.business_reviews.insert_one({"business_id": near.json()["id"], "rating": 4}))

        listed = client.get("/api/v1/businesses/nearby?latitude=47.02&longitude=28.83&limit=1", headers=HEADERS_A)
        assert listed.status_code == 200, listed.text
        page = listed.json()
        assert page["has_more"] is True
        assert page["items"][0]["name"] == "Near Clinic"
        assert page["items"][0]["distance_km"] == 0.0
        assert page["items"][0]["rating"] == 4.5

        anywhere = client.get("/api/v1/businesses/nearby?anywhere=true&offset=0&limit=15", headers=HEADERS_A)
        names = [item["name"] for item in anywhere.json()["items"]]
        assert names == ["Far Clinic", "Near Clinic"]


def test_nearby_treats_24_00_as_end_of_day(client, mock_db):
    """00:00–24:00 is open all day. A weekend with no slots still has a next open."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    asyncio.run(
        mock_db.businesses.insert_one(
            _legacy(
                "Ciavdar Grup Clinica Veterinară",
                opening_hours={
                    "mon": [{"open": "00:00", "close": "24:00"}],
                    "tue": [{"open": "00:00", "close": "24:00"}],
                    "wed": [{"open": "00:00", "close": "24:00"}],
                    "thu": [{"open": "00:00", "close": "24:00"}],
                    "fri": [{"open": "00:00", "close": "24:00"}],
                    "sat": [],
                    "sun": [],
                },
                location={"type": "Point", "coordinates": [28.86, 47.02]},
            )
        )
    )
    zone = ZoneInfo("Europe/Chisinau")

    class Clock(datetime):
        current = datetime(2026, 10, 3, 9, 41, tzinfo=zone)

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.current
            return cls.current.astimezone(tz)

    with patch("app.business_docs.datetime", Clock):
        saturday = client.get("/api/v1/businesses/nearby?latitude=47.02&longitude=28.86", headers=HEADERS_A)
        assert saturday.status_code == 200, saturday.text
        closed = saturday.json()["items"][0]
        assert closed["open_now"] is False
        assert closed["opens_at"] == "00:00"
        assert closed["next_open_day"] == "mon"

        Clock.current = datetime(2026, 9, 30, 12, 0, tzinfo=zone)
        weekday = client.get("/api/v1/businesses/nearby?latitude=47.02&longitude=28.86", headers=HEADERS_A)
        opened = weekday.json()["items"][0]
        assert opened["open_now"] is True
        assert opened["open_24_7"] is False
        assert opened["closes_at"] == "24:00"


def test_nearby_marks_all_day_every_day_as_open_24_7(client, mock_db):
    day = [{"open": "00:00", "close": "24:00"}]
    asyncio.run(
        mock_db.businesses.insert_one(
            _legacy("Ciavdar", opening_hours={d: day for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}, location={"type": "Point", "coordinates": [28.86, 47.02]})
        )
    )
    listed = client.get("/api/v1/businesses/nearby?latitude=47.02&longitude=28.86", headers=HEADERS_A)
    assert listed.status_code == 200, listed.text
    place = listed.json()["items"][0]
    assert place["open_now"] is True
    assert place["open_24_7"] is True
    assert place["closes_at"] is None


def test_place_detail_returns_contact_hours_and_reviews(client, mock_db):
    from datetime import datetime, timezone

    doc = _legacy("Zoomama")
    asyncio.run(mock_db.businesses.insert_one(doc))
    asyncio.run(mock_db.users.insert_one({"firebase_uid": "uid_user_a", "name": "Laurie", "photo_url": "https://example.com/laurie.jpg"}))
    asyncio.run(
        mock_db.business_reviews.insert_one(
            {
                "business_id": str(doc["_id"]),
                "user_id": "uid_user_a",
                "rating": 5,
                "comment": "Very kind and caring staff.",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        )
    )
    response = client.get(f"/api/v1/businesses/{doc['_id']}?latitude=47.02&longitude=28.83", headers=HEADERS_A)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Zoomama"
    assert body["address"] == "Str. București 45"
    assert body["phone"] == ["+373 69 123 456", "+373 22 000 111"]
    assert body["website"] == "https://zoomama.md"
    assert body["instagram"] == "@zoomama"
    assert body["opening_hours"]["mon"][0]["open"] == "09:00"
    assert body["distance_km"] == 0.0
    assert body["rating"] == 5.0
    assert body["reviews"][0]["author_name"] == "Laurie"
    assert body["reviews"][0]["author_photo"] == "https://example.com/laurie.jpg"
    assert body["locations"][0]["categories"] == ["veterinarian"]

    missing = client.get("/api/v1/businesses/not-an-id", headers=HEADERS_A)
    assert missing.status_code == 404


def test_owner_review_is_saved_once_and_updates_the_rating(client, mock_db):
    doc = _legacy("Zoomama")
    asyncio.run(mock_db.businesses.insert_one(doc))
    asyncio.run(mock_db.users.insert_one({"firebase_uid": "uid_user_a", "name": "Yoav", "photo_url": "https://example.com/yoav.jpg"}))
    created = client.post(f"/api/v1/businesses/{doc['_id']}/reviews", headers=HEADERS_A, json={"rating": 4, "comment": "  Kind staff.  "})
    assert created.status_code == 200, created.text
    assert created.json()["comment"] == "Kind staff."
    assert created.json()["author_name"] == "Yoav"
    updated = client.post(f"/api/v1/businesses/{doc['_id']}/reviews", headers=HEADERS_A, json={"rating": 5, "comment": "Even better."})
    assert updated.json()["id"] == created.json()["id"]
    assert mock_db.business_reviews._col.count_documents({"business_id": str(doc["_id"])}) == 1
    listed = client.get(f"/api/v1/businesses/{doc['_id']}", headers=HEADERS_A).json()
    assert listed["rating"] == 5.0
    assert listed["reviews"][0]["is_mine"] is True


def test_owner_can_delete_their_review_and_the_rating_clears(client, mock_db):
    doc = _legacy("Zoomama")
    asyncio.run(mock_db.businesses.insert_one(doc))
    client.post(f"/api/v1/businesses/{doc['_id']}/reviews", headers=HEADERS_A, json={"rating": 4, "comment": "Kind staff."})
    other = client.delete(f"/api/v1/businesses/{doc['_id']}/reviews", headers=HEADERS_B)
    assert other.status_code == 404
    deleted = client.delete(f"/api/v1/businesses/{doc['_id']}/reviews", headers=HEADERS_A)
    assert deleted.status_code == 204
    listed = client.get(f"/api/v1/businesses/{doc['_id']}", headers=HEADERS_A).json()
    assert listed["rating"] is None
    assert listed["reviews"] == []


def test_reviews_page_is_fifteen_and_the_cursor_continues(client, mock_db):
    from datetime import datetime, timedelta, timezone

    doc = _legacy("Zoomama")
    asyncio.run(mock_db.businesses.insert_one(doc))
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for index in range(16):
        asyncio.run(
            mock_db.business_reviews.insert_one(
                {"_id": ObjectId(), "business_id": str(doc["_id"]), "user_id": f"uid_{index}", "rating": 5, "comment": f"Review {index}", "created_at": start + timedelta(minutes=index)}
            )
        )
    first = client.get(f"/api/v1/businesses/{doc['_id']}/reviews", headers=HEADERS_A)
    page = first.json()
    assert len(page) == 15
    assert page[0]["comment"] == "Review 15"
    second = client.get(f"/api/v1/businesses/{doc['_id']}/reviews", headers=HEADERS_A, params={"cursor": page[-1]["id"]})
    assert [item["comment"] for item in second.json()] == ["Review 0"]


def test_report_emails_admins_the_review_author_and_reporter(client, mock_db):
    doc = _legacy("Zoomama", phone=["+373 22 000 000"], website="https://zoomama.example")
    asyncio.run(mock_db.businesses.insert_one(doc))
    asyncio.run(mock_db.users.insert_one({"firebase_uid": "uid_user_a", "name": "Yoav", "email": "yoav@example.com", "phone": "+1 555 010 0101"}))
    asyncio.run(mock_db.users.insert_one({"firebase_uid": "uid_user_b", "name": "Mia", "email": "mia@example.com", "phone": "+1 555 010 0199"}))
    created = client.post(f"/api/v1/businesses/{doc['_id']}/reviews", headers=HEADERS_A, json={"rating": 2, "comment": "They ignored my dog."})
    review_id = created.json()["id"]
    own = client.post(f"/api/v1/businesses/{doc['_id']}/reviews/{review_id}/report", headers=HEADERS_A, json={"reason": "spam"})
    assert own.status_code == 403
    with patch("app.routers.businesses.send_review_report_email") as send:
        with patch.object(settings, "RAGLY_ADMIN_EMAILS", "admin@ragly.cloud"), patch.object(settings, "SUPPORT_EMAIL", "pettoservices@gmail.com"):
            reported = client.post(f"/api/v1/businesses/{doc['_id']}/reviews/{review_id}/report", headers=HEADERS_B, json={"reason": "offensive"})
    assert reported.status_code == 204, reported.text
    assert {call.args[0] for call in send.call_args_list} == {"admin@ragly.cloud", "pettoservices@gmail.com"}
    mailed = "\n".join(send.call_args.kwargs["lines"])
    assert send.call_args.kwargs["subject"] == "Ragly: review report — Zoomama"
    assert "They ignored my dog." in mailed
    assert "yoav@example.com" in mailed
    assert "mia@example.com" in mailed
    assert "Report id:" in mailed
