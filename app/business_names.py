"""Stable key for a business name. Matching keys are the same listing."""

import re

_SPACE = re.compile(r"\s+")


def business_name_key(name: str) -> str:
    """Ignore case and repeated spaces so 'Zoomama' and 'zoomama' are one name."""
    return _SPACE.sub(" ", name.strip()).casefold()
