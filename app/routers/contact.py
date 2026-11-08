"""Public contact form from the business website footer. No login."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.contact import clean_email, clean_phone
from app.core.config import settings
from app.core.database import get_database
from app.core.email_service import send_review_report_email
from app.core.errors import ErrorCode, raise_api_error

router = APIRouter(prefix="/contact", tags=["contact"])

_EMAIL_COOLDOWN = timedelta(hours=6)
_IP_HOUR_LIMIT = 8


class ContactLeadIn(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    website: str = ""


class ContactLeadOut(BaseModel):
    ok: bool = True


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _clean_name(value: str) -> str | None:
    name = " ".join(value.split())
    if len(name) < 2 or len(name) > 80:
        return None
    lowered = name.lower()
    if "http://" in lowered or "https://" in lowered or "@" in name:
        return None
    return name


@router.post("", response_model=ContactLeadOut)
async def submit_contact(body: ContactLeadIn, request: Request, db: AsyncIOMotorDatabase = Depends(get_database)):
    if body.website.strip():
        return ContactLeadOut()

    name = _clean_name(body.name)
    if not name:
        raise_api_error(400, ErrorCode.CONTACT_INVALID)
    try:
        email = clean_email(body.email)
    except ValueError:
        raise_api_error(400, ErrorCode.INVALID_EMAIL)
    if not email:
        raise_api_error(400, ErrorCode.INVALID_EMAIL)

    phone_text = body.phone.strip()
    phone = ""
    if phone_text:
        parsed = clean_phone(phone_text)
        if parsed is None:
            raise_api_error(400, ErrorCode.CONTACT_INVALID)
        phone = parsed

    now = datetime.now(timezone.utc)
    ip = _client_ip(request)
    recent_email = await db.contact_leads.find_one(
        {"email": email, "created_at": {"$gte": now - _EMAIL_COOLDOWN}},
        {"_id": 1},
    )
    if recent_email:
        raise_api_error(429, ErrorCode.CONTACT_TOO_SOON, headers={"Retry-After": "3600"})
    recent_ip = await db.contact_leads.count_documents(
        {"ip": ip, "created_at": {"$gte": now - timedelta(hours=1)}},
    )
    if ip and recent_ip >= _IP_HOUR_LIMIT:
        raise_api_error(429, ErrorCode.CONTACT_TOO_SOON, headers={"Retry-After": "3600"})

    await db.contact_leads.insert_one(
        {
            "name": name,
            "email": email,
            "phone": phone or None,
            "ip": ip or None,
            "created_at": now,
        }
    )

    lines = [f"Name: {name}", f"Email: {email}"]
    if phone:
        lines.append(f"Phone: {phone}")
    recipients = set(settings.ragly_admin_emails)
    if settings.SUPPORT_EMAIL.strip():
        recipients.add(settings.SUPPORT_EMAIL.strip().lower())
    for admin_email in sorted(recipients):
        send_review_report_email(
            admin_email,
            subject=f"Ragly: website enquiry — {name}",
            lines=lines,
        )
    return ContactLeadOut()
