"""
main.py — FastAPI application entry point.

Startup sequence:
  1. Connect to MongoDB and create indexes
  2. Initialize Firebase Admin SDK
  3. Mount all routers under /api/v1

Health check at GET /health (unauthenticated) for Cloud Run probes.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.database import connect_to_db, close_db_connection
from app.core.firebase import initialize_firebase
from app.routers import users, pets, vaccinations, medical_records, reminders


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run startup tasks before serving, cleanup on shutdown."""
    await connect_to_db()
    initialize_firebase()
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

# All protected routes live under /api/v1
PREFIX = "/api/v1"
app.include_router(users.router, prefix=PREFIX)
app.include_router(pets.router, prefix=PREFIX)
app.include_router(vaccinations.router, prefix=PREFIX)
app.include_router(medical_records.router, prefix=PREFIX)
app.include_router(reminders.router, prefix=PREFIX)


@app.get("/health", tags=["health"])
async def health():
    """Liveness probe for Cloud Run. No auth required."""
    return {"status": "ok"}
