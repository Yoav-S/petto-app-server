"""
push.py — Send notifications through the Expo Push Service.

Why Expo Push (vs. raw FCM/APNs): one HTTP endpoint delivers to both iOS and
Android using the Expo push tokens the app already generates. Works unchanged
the moment the app runs as a real build (Expo Go can't receive remote push).

This module is transport-only: it takes ready-made messages and returns Expo's
"tickets" (one per message). The dispatcher decides what to send and how to
react to failures (e.g. pruning dead tokens).
"""
import asyncio
import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger("petto")

_EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_EXPO_RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"
_CHUNK_SIZE = 100  # Expo accepts up to 100 messages per request.


def _auth_headers() -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    }
    if settings.EXPO_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {settings.EXPO_ACCESS_TOKEN}"
    return headers


def _token_preview(token: str) -> str:
    if not token:
        return "(empty)"
    if len(token) <= 24:
        return token
    return f"{token[:22]}…{token[-6:]}"


def _normalize_tickets(data: Any, expected: int) -> list[dict]:
    """Expo may return a single object or a list depending on payload shape."""
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    logger.warning("Expo push: unexpected data type %s (expected=%d)", type(data).__name__, expected)
    return []


async def send_expo_push(messages: list[dict]) -> list[dict]:
    """
    POST messages to Expo and return the flat list of ticket objects.

    Each message: {"to": <ExpoPushToken>, "title", "body", "data", "sound"}.
    Each ticket:  {"status": "ok", "id": "..."} or
                  {"status": "error", "message": "...", "details": {"error": "DeviceNotRegistered"}}.
    """
    if not messages:
        return []

    headers = _auth_headers()
    tickets: list[dict] = []

    async with httpx.AsyncClient(timeout=20.0) as client:
        for start in range(0, len(messages), _CHUNK_SIZE):
            chunk = messages[start : start + _CHUNK_SIZE]
            previews = [_token_preview(str(m.get("to", ""))) for m in chunk]
            logger.info(
                "Expo push: posting chunk size=%d tokens=%s titles=%s",
                len(chunk),
                previews,
                [m.get("title") for m in chunk],
            )
            try:
                response = await client.post(_EXPO_PUSH_URL, json=chunk, headers=headers)
            except httpx.HTTPError as exc:
                logger.exception("Expo push HTTP failure: %s", exc)
                raise

            body_text = response.text
            if response.status_code >= 400:
                logger.error(
                    "Expo push HTTP %s body=%s",
                    response.status_code,
                    body_text[:800],
                )
            response.raise_for_status()

            payload = response.json()
            if payload.get("errors"):
                logger.error("Expo push response errors=%s", payload.get("errors"))

            chunk_tickets = _normalize_tickets(payload.get("data"), len(chunk))
            tickets.extend(chunk_tickets)

            for i, ticket in enumerate(chunk_tickets):
                token = chunk[i].get("to") if i < len(chunk) else "?"
                status = ticket.get("status")
                if status == "ok":
                    logger.info(
                        "Expo ticket ok token=%s ticket_id=%s",
                        _token_preview(str(token)),
                        ticket.get("id"),
                    )
                else:
                    logger.error(
                        "Expo ticket ERROR token=%s status=%s message=%s details=%s",
                        _token_preview(str(token)),
                        status,
                        ticket.get("message"),
                        ticket.get("details"),
                    )

    ok = sum(1 for t in tickets if t.get("status") == "ok")
    logger.info("Expo push: sent=%d ok=%d errors=%d", len(tickets), ok, len(tickets) - ok)

    # Receipts arrive a moment later — fetch them so FCM/device failures show in logs.
    ticket_ids = [t.get("id") for t in tickets if t.get("status") == "ok" and t.get("id")]
    if ticket_ids:
        try:
            await asyncio.sleep(1.5)
            await _log_expo_receipts(ticket_ids)
        except Exception:
            logger.exception("Expo receipts fetch failed")

    return tickets


async def _log_expo_receipts(ticket_ids: list[str]) -> None:
    """Pull delivery receipts for successful tickets (shows FCM/Android failures)."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            _EXPO_RECEIPTS_URL,
            json={"ids": ticket_ids},
            headers=_auth_headers(),
        )
        if response.status_code >= 400:
            logger.error(
                "Expo receipts HTTP %s body=%s",
                response.status_code,
                response.text[:800],
            )
            return
        data = response.json().get("data") or {}
        if not data:
            logger.info("Expo receipts: empty (still processing) ids=%s", ticket_ids)
            return
        for ticket_id, receipt in data.items():
            status = receipt.get("status")
            if status == "ok":
                logger.info("Expo receipt ok ticket_id=%s", ticket_id)
            else:
                logger.error(
                    "Expo receipt ERROR ticket_id=%s status=%s message=%s details=%s",
                    ticket_id,
                    status,
                    receipt.get("message"),
                    receipt.get("details"),
                )


def is_dead_token_ticket(ticket: dict) -> bool:
    """True if Expo says this token is no longer valid and should be removed."""
    return (
        ticket.get("status") == "error"
        and ticket.get("details", {}).get("error") == "DeviceNotRegistered"
    )
