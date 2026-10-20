"""Detect a business logo from its bytes. The declared file type is not trusted."""

import re

# content type -> storage extension
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/tiff": ".tif",
    "image/x-icon": ".ico",
    "image/heic": ".heic",
    "image/heif": ".heif",
}

_BAD_SVG = re.compile(
    r"<\s*(?:script|foreignObject|iframe|embed|object|link|meta|handler)\b"
    r"|javascript\s*:"
    r"|\son[a-z]+\s*="
    r"""|href\s*=\s*['"]\s*(?:https?:)?//""",
    re.IGNORECASE,
)


def identify_image(content: bytes) -> tuple[str, str] | None:
    """Return the content type and extension, or None when the file is not a logo we store."""
    content_type = _detect(content)
    if not content_type:
        return None
    extension = IMAGE_EXTENSIONS.get(content_type)
    if not extension:
        return None
    return content_type, extension


def _detect(content: bytes) -> str | None:
    if len(content) >= 3 and content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(content) >= 8 and content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(content) >= 6 and content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if len(content) >= 2 and content[:2] == b"BM":
        return "image/bmp"
    if len(content) >= 4 and content[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if len(content) >= 4 and content[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    brand = _ftyp_brand(content)
    if brand in {b"avif", b"avis"}:
        return "image/avif"
    if brand in {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis"}:
        return "image/heic"
    if brand in {b"mif1", b"msf1"}:
        return "image/heif"
    if _safe_svg(content):
        return "image/svg+xml"
    return None


def _ftyp_brand(content: bytes) -> bytes | None:
    if len(content) < 12 or content[4:8] != b"ftyp":
        return None
    return content[8:12]


def _safe_svg(content: bytes) -> bool:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    start = text.lstrip("\ufeff").lstrip().lower()
    if not (start.startswith("<svg") or start.startswith("<?xml")):
        return False
    if "<svg" not in start:
        return False
    if _BAD_SVG.search(text):
        return False
    return True
