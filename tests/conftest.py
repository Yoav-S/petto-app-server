"""
conftest.py — Shared test fixtures for all test files.

Strategy:
  - Use a thin async wrapper around mongomock's synchronous client.
    Motor's awaitable API (find_one, insert_one, etc.) is replicated via
    AsyncWrapper so that routers that do `await db.pets.insert_one(...)` work.
  - Override the get_database() FastAPI dependency to return the mock DB.
  - Patch verify_firebase_token to return a fixed uid without network calls.

Two users are pre-defined:
  USER_A_UID = "uid_user_a"
  USER_B_UID = "uid_user_b"
"""
import asyncio
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch
import mongomock

from app.main import app
from app.core.database import get_database

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_A_UID = "uid_user_a"
USER_B_UID = "uid_user_b"
TOKEN_A = "token_user_a"
TOKEN_B = "token_user_b"


def auth_headers(token: str) -> dict:
    """Return Authorization header dict for a test token."""
    return {"Authorization": f"Bearer {token}"}


HEADERS_A = auth_headers(TOKEN_A)
HEADERS_B = auth_headers(TOKEN_B)


# ---------------------------------------------------------------------------
# Async wrapper around synchronous mongomock
#
# Motor's Collection API is async; mongomock's is sync.
# This lightweight wrapper makes every method awaitable so the production
# router code (`await db.pets.insert_one(...)`) works unchanged in tests.
# ---------------------------------------------------------------------------

class AsyncCursor:
    """Wraps a mongomock cursor to provide async to_list()."""
    def __init__(self, cursor):
        self._cursor = cursor

    def sort(self, *args, **kwargs):
        return AsyncCursor(self._cursor.sort(*args, **kwargs))

    async def to_list(self, length=None):
        return list(self._cursor)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._cursor)
        except StopIteration:
            raise StopAsyncIteration


class AsyncCollection:
    """Wraps a mongomock Collection, making every method awaitable."""
    def __init__(self, col):
        self._col = col

    async def find_one(self, *args, **kwargs):
        return self._col.find_one(*args, **kwargs)

    def find(self, *args, **kwargs):
        sort = kwargs.pop("sort", None)
        cursor = self._col.find(*args, **kwargs)
        if sort:
            cursor = cursor.sort(sort)
        return AsyncCursor(cursor)

    async def insert_one(self, doc):
        return self._col.insert_one(doc)

    async def update_one(self, *args, **kwargs):
        return self._col.update_one(*args, **kwargs)

    async def delete_one(self, *args, **kwargs):
        return self._col.delete_one(*args, **kwargs)

    async def delete_many(self, *args, **kwargs):
        return self._col.delete_many(*args, **kwargs)

    async def create_index(self, *args, **kwargs):
        return self._col.create_index(*args, **kwargs)


class AsyncDatabase:
    """Wraps a mongomock Database, returning AsyncCollection on attribute access."""
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return AsyncCollection(getattr(self._db, name))

    def __getitem__(self, name):
        return AsyncCollection(self._db[name])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def mock_db():
    """Fresh async-wrapped in-memory MongoDB per test function."""
    client = mongomock.MongoClient()
    return AsyncDatabase(client["petto_test"])


# ---------------------------------------------------------------------------
# TestClient with mocked auth + DB
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def client(mock_db):
    """
    FastAPI TestClient with:
      - get_database() returning mock_db
      - verify_firebase_token patched to map TOKEN_A → USER_A_UID, TOKEN_B → USER_B_UID
    """
    def mock_verify(token: str) -> dict:
        mapping = {TOKEN_A: USER_A_UID, TOKEN_B: USER_B_UID}
        uid = mapping.get(token)
        if uid is None:
            raise ValueError("Invalid token")
        return {"uid": uid, "email": f"{uid}@test.com"}

    app.dependency_overrides[get_database] = lambda: mock_db

    with patch("app.middleware.auth.verify_firebase_token", side_effect=mock_verify):
        with patch("app.core.firebase.initialize_firebase"):
            with TestClient(app) as c:
                yield c

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_pet(client, headers: dict, name: str = "Buddy", pet_type: str = "Dog") -> dict:
    """Create a pet and return the response JSON."""
    r = client.post("/api/v1/pets", json={"name": name, "type": pet_type}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def make_reminder(client, pet_id: str, headers: dict, **kwargs) -> dict:
    """Create a reminder with sensible defaults."""
    payload = {
        "title": "Test reminder",
        "date": "2099-01-01",
        "time": "09:00",
        "repeat": "off",
        **kwargs,
    }
    r = client.post(f"/api/v1/pets/{pet_id}/reminders", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def make_vaccination(client, pet_id: str, headers: dict, **kwargs) -> dict:
    """Create a vaccination with sensible defaults."""
    payload = {
        "name": "Rabies",
        "date": "2025-01-01",
        **kwargs,
    }
    r = client.post(f"/api/v1/pets/{pet_id}/vaccinations", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def make_medical_record(client, pet_id: str, headers: dict, title: str = "Allergy") -> dict:
    """Create a health condition."""
    r = client.post(
        f"/api/v1/pets/{pet_id}/medical-records",
        json={"title": title},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()
