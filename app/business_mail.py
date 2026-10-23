"""Emails to business owners, in the language they use on the website.

Delivery stays in email_service. This module only chooses the words.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.email_service import EmailDeliveryError, send_business_owner_email

logger = logging.getLogger("petto")

_FIELD_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "name": "Business name",
        "phone": "Phone",
        "email": "Email",
        "description": "About",
        "website": "Website",
        "instagram": "Instagram",
        "photo": "Photo",
        "address": "Address",
        "city": "City",
        "location": "Map pin",
        "services": "Services",
        "hours": "Opening hours",
        "general": "Note",
    },
    "ro": {
        "name": "Numele afacerii",
        "phone": "Telefon",
        "email": "Email",
        "description": "Despre",
        "website": "Site web",
        "instagram": "Instagram",
        "photo": "Fotografie",
        "address": "Adresă",
        "city": "Oraș",
        "location": "Punct pe hartă",
        "services": "Servicii",
        "hours": "Program",
        "general": "Notă",
    },
    "ru": {
        "name": "Название бизнеса",
        "phone": "Телефон",
        "email": "Email",
        "description": "О бизнесе",
        "website": "Сайт",
        "instagram": "Instagram",
        "photo": "Фото",
        "address": "Адрес",
        "city": "Город",
        "location": "Точка на карте",
        "services": "Услуги",
        "hours": "Часы работы",
        "general": "Примечание",
    },
    "he": {
        "name": "שם העסק",
        "phone": "טלפון",
        "email": "אימייל",
        "description": "אודות",
        "website": "אתר",
        "instagram": "אינסטגרם",
        "photo": "תמונה",
        "address": "כתובת",
        "city": "עיר",
        "location": "נקודה במפה",
        "services": "שירותים",
        "hours": "שעות פעילות",
        "general": "הערה",
    },
}

_COPY: dict[str, dict[str, dict[str, str]]] = {
    "received": {
        "en": {
            "subject": "Ragly: {name} is waiting for review",
            "intro": "We received the branch at {address} for {name}. It stays off the app until a Ragly admin reviews it. We usually answer within two working days.",
            "action": "Open your dashboard",
        },
        "ro": {
            "subject": "Ragly: {name} așteaptă verificarea",
            "intro": "Am primit filiala de la {address} pentru {name}. Nu apare în aplicație până când un administrator Ragly o verifică. De obicei răspundem în două zile lucrătoare.",
            "action": "Deschide panoul",
        },
        "ru": {
            "subject": "Ragly: {name} ожидает проверки",
            "intro": "Мы получили филиал по адресу {address} для {name}. Он не появится в приложении, пока администратор Ragly его не проверит. Обычно мы отвечаем в течение двух рабочих дней.",
            "action": "Открыть панель",
        },
        "he": {
            "subject": "Ragly: {name} ממתין לבדיקה",
            "intro": "קיבלנו את הסניף בכתובת {address} עבור {name}. הוא לא יופיע באפליקציה עד שמנהל Ragly יבדוק אותו. בדרך כלל אנחנו עונים תוך יומיים עבודה.",
            "action": "פתיחת לוח הבקרה",
        },
    },
    "published": {
        "en": {
            "subject": "Ragly: {name} is live",
            "intro": "{name} is published. Pet owners can now find the branch at {address} in the Ragly app.",
            "action": "Open your dashboard",
        },
        "ro": {
            "subject": "Ragly: {name} este activ",
            "intro": "{name} a fost publicat. Proprietarii de animale pot găsi acum filiala de la {address} în aplicația Ragly.",
            "action": "Deschide panoul",
        },
        "ru": {
            "subject": "Ragly: {name} опубликован",
            "intro": "{name} опубликован. Владельцы питомцев теперь могут найти филиал по адресу {address} в приложении Ragly.",
            "action": "Открыть панель",
        },
        "he": {
            "subject": "Ragly: {name} פורסם",
            "intro": "{name} פורסם. בעלי חיות מחמד יכולים עכשיו למצוא את הסניף בכתובת {address} באפליקציית Ragly.",
            "action": "פתיחת לוח הבקרה",
        },
    },
    "branch_published": {
        "en": {
            "subject": "Ragly: your branch at {address} is live",
            "intro": "The branch of {name} at {address} is published and visible in the Ragly app.",
            "action": "Open your dashboard",
        },
        "ro": {
            "subject": "Ragly: filiala de la {address} este activă",
            "intro": "Filiala {name} de la {address} a fost publicată și este vizibilă în aplicația Ragly.",
            "action": "Deschide panoul",
        },
        "ru": {
            "subject": "Ragly: филиал по адресу {address} опубликован",
            "intro": "Филиал {name} по адресу {address} опубликован и виден в приложении Ragly.",
            "action": "Открыть панель",
        },
        "he": {
            "subject": "Ragly: הסניף בכתובת {address} פורסם",
            "intro": "הסניף של {name} בכתובת {address} פורסם וגלוי באפליקציית Ragly.",
            "action": "פתיחת לוח הבקרה",
        },
    },
    "rejected": {
        "en": {
            "subject": "Ragly: {name} needs changes",
            "intro": "The branch of {name} at {address} was not published yet. Fix the points below and send it again.",
            "action": "Open your dashboard",
        },
        "ro": {
            "subject": "Ragly: {name} necesită modificări",
            "intro": "Filiala {name} de la {address} nu a fost publicată încă. Corectează punctele de mai jos și trimite din nou.",
            "action": "Deschide panoul",
        },
        "ru": {
            "subject": "Ragly: {name} требует изменений",
            "intro": "Филиал {name} по адресу {address} пока не опубликован. Исправьте пункты ниже и отправьте снова.",
            "action": "Открыть панель",
        },
        "he": {
            "subject": "Ragly: {name} דורש שינויים",
            "intro": "הסניף של {name} בכתובת {address} עדיין לא פורסם. תקנו את הנקודות למטה ושלחו שוב.",
            "action": "פתיחת לוח הבקרה",
        },
    },
}


def _locale(value: str | None) -> str:
    code = (value or "en").strip().lower().split("-")[0]
    return code if code in _FIELD_LABELS else "en"


def note_lines(field_notes: dict[str, str], locale: str | None) -> list[str]:
    """`field: message` pairs with the field named in the owner's language."""
    labels = _FIELD_LABELS[_locale(locale)]
    lines: list[str] = []
    for key, message in field_notes.items():
        base, _, rest = key.partition(":")
        label = labels.get(base, base)
        if base == "service" and rest:
            label = f"{labels['services']} · {rest}"
        lines.append(f"{label}: {message}")
    return lines


def send_owner_update(
    email: str | None,
    *,
    kind: str,
    locale: str | None,
    business_name: str,
    address: str,
    field_notes: dict[str, str] | None = None,
) -> None:
    """Email the owner about a request. Failures are logged, never raised."""
    if not email or kind not in _COPY:
        return
    lang = _locale(locale)
    copy = _COPY[kind][lang]
    name = business_name or "Ragly"
    where = address or "—"
    dashboard = settings.BUSINESS_APP_URL.strip().rstrip("/") + "/dashboard"
    try:
        send_business_owner_email(
            email,
            business_name=name,
            subject=copy["subject"].format(name=name, address=where),
            intro=copy["intro"].format(name=name, address=where),
            lines=note_lines(field_notes or {}, lang) or None,
            action_url=dashboard,
            action_label=copy["action"],
            rtl=lang == "he",
        )
    except EmailDeliveryError:
        logger.exception("Owner email failed for %s", email)
