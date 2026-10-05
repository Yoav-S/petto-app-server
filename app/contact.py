"""Shared checks for phones, emails, websites, and Instagram names."""

import re

_PHONE = re.compile(r"^\+?[\d\s().-]+$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}(?::\d+)?$")
_HANDLE = re.compile(r"[A-Za-z0-9._]{1,30}")


def clean_phone(value: str) -> str | None:
    """A dialable number: digits, or the same with spaces, dashes, and a leading +."""
    text = re.sub(r"\s+", " ", value.strip())
    if not text or len(text) > 32 or not _PHONE.fullmatch(text):
        return None
    if text.count("+") > 1 or ("+" in text and not text.startswith("+")):
        return None
    digits = re.sub(r"\D", "", text)
    if not 7 <= len(digits) <= 15:
        return None
    return text


def clean_phone_list(value: list[str], *, required: bool) -> list[str]:
    cleaned: list[str] = []
    for item in value:
        text = item.strip() if isinstance(item, str) else ""
        if not text:
            continue
        phone = clean_phone(text)
        if phone is None:
            raise ValueError("phone")
        cleaned.append(phone)
    if required and not cleaned:
        raise ValueError("phone")
    return cleaned


def clean_email(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > 320 or not _EMAIL.fullmatch(text):
        raise ValueError("email")
    return text


def clean_website(value: str | None) -> str | None:
    """Empty stays empty. example.com becomes https://example.com."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > 300 or any(char.isspace() for char in text):
        raise ValueError("website")
    lowered = text.lower()
    if "://" in lowered:
        if not lowered.startswith(("http://", "https://")):
            raise ValueError("website")
    else:
        text = "https://" + text
        lowered = text.lower()
    host = lowered.split("://", 1)[1].split("/")[0].split("?")[0].split("#")[0]
    if host.startswith("www."):
        host = host[4:]
    if not host or not _HOST.fullmatch(host):
        raise ValueError("website")
    return text


def clean_instagram(value: str | None) -> str | None:
    """Empty stays empty. A name, @name, or an Instagram link becomes @name."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    lowered = text.lower()
    for prefix in (
        "https://www.instagram.com/",
        "http://www.instagram.com/",
        "https://instagram.com/",
        "http://instagram.com/",
        "www.instagram.com/",
        "instagram.com/",
    ):
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    handle = text.strip().strip("/")
    if handle.startswith("@"):
        handle = handle[1:]
    handle = handle.split("?")[0].split("/")[0]
    if not _HANDLE.fullmatch(handle):
        raise ValueError("instagram")
    return f"@{handle}"
