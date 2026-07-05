"""
main.py — FastAPI application entry point.

Startup sequence:
  1. Connect to MongoDB and create indexes
  2. Initialize Firebase Admin SDK
  3. Mount all routers under /api/v1

Health check at GET /health (unauthenticated) for Cloud Run probes.
"""
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("petto")

from app.core.database import connect_to_db, close_db_connection
from app.core.firebase import initialize_firebase
from app.core.gcp_secrets import resolve_resend_credentials
from app.routers import (
    users,
    pets,
    vaccinations,
    medical_records,
    reminders,
    auth,
    notifications,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run startup tasks before serving, cleanup on shutdown."""
    await connect_to_db()
    initialize_firebase()
    resend_key, resend_from, source = resolve_resend_credentials()
    if resend_key and resend_from:
        logger.info(
            "OTP email: Resend configured (from=%s, source=%s)",
            resend_from,
            source,
        )
    else:
        key_raw = os.environ.get("RESEND_API_KEY", "")
        from_raw = os.environ.get("RESEND_FROM_EMAIL", "")
        logger.warning(
            "OTP email: not configured — RESEND_API_KEY env len=%d, "
            "RESEND_FROM_EMAIL env=%r. Add secrets RESEND_API_KEY and "
            "RESEND_FROM_EMAIL in Secret Manager.",
            len(key_raw.strip()),
            from_raw.strip()[:80] if from_raw else "",
        )
    yield
    await close_db_connection()


app = FastAPI(
    title="Petto API",
    version="1.0.0",
    description="Simple, reliable data storage for pet medical history.",
    lifespan=lifespan,
    # Disable Swagger in production if needed (currently open for development)
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # Restrict to client origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    response = await call_next(request)
    logger.info("%s %s -> %s", request.method, request.url.path, response.status_code)
    return response

# All protected routes live under /api/v1
PREFIX = "/api/v1"
app.include_router(auth.router, prefix=PREFIX)
app.include_router(users.router, prefix=PREFIX)
app.include_router(pets.router, prefix=PREFIX)
app.include_router(vaccinations.router, prefix=PREFIX)
app.include_router(medical_records.router, prefix=PREFIX)
app.include_router(reminders.router, prefix=PREFIX)
app.include_router(notifications.router, prefix=PREFIX)


@app.get("/health", tags=["health"])
async def health():
    """Liveness probe for Cloud Run. No auth required."""
    return {"status": "ok"}
