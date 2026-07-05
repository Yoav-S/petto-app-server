"""
push.py — Send notifications through the Expo Push Service.

Why Expo Push (vs. raw FCM/APNs): one HTTP endpoint delivers to both iOS and
Android using the Expo push tokens the app already generates. Works unchanged
the moment the app runs as a real build (Expo Go can't receive remote push).

This module is transport-only: it takes ready-made messages and returns Expo's
"tickets" (one per message). The dispatcher decides what to send and how to
react to failures (e.g. pruning dead tokens).
"""
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("petto")

_EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_CHUNK_SIZE = 100  # Expo accepts up to 100 messages per request.


async def send_expo_push(messages: list[dict]) -> list[dict]:
    """
    POST messages to Expo and return the flat list of ticket objects.

    Each message: {"to": <ExpoPushToken>, "title", "body", "data", "sound"}.
    Each ticket:  {"status": "ok", "id": "..."} or
                  {"status": "error", "message": "...", "details": {"error": "DeviceNotRegistered"}}.

    Network/HTTP errors are logged and surfaced to the caller.
    """
    if not messages:
        return []

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings.EXPO_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {settings.EXPO_ACCESS_TOKEN}"

    tickets: list[dict] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for start in range(0, len(messages), _CHUNK_SIZE):
            chunk = messages[start : start + _CHUNK_SIZE]
            response = await client.post(_EXPO_PUSH_URL, json=chunk, headers=headers)
            response.raise_for_status()
            data = response.json().get("data", [])
            tickets.extend(data)

    ok = sum(1 for t in tickets if t.get("status") == "ok")
    logger.info("Expo push: sent=%d ok=%d errors=%d", len(tickets), ok, len(tickets) - ok)
    return tickets


def is_dead_token_ticket(ticket: dict) -> bool:
    """True if Expo says this token is no longer valid and should be removed."""
    return (
        ticket.get("status") == "error"
        and ticket.get("details", {}).get("error") == "DeviceNotRegistered"
    )
