"""Path/URL-safe tokens for anything a provider hands us that ends up in a
file name: series slugs, chapter ids, volume ids.

Provider data comes from web pages, so it is untrusted: a slug of
`../../etc` must never become a path component.
"""
from __future__ import annotations

import re

_BAD_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_slug(value, fallback: str = "") -> str:
    """Keep [A-Za-z0-9._-], turn every other run into `-`, no leading dots."""
    s = _BAD_RE.sub("-", str(value or "").strip()).strip(".-")
    return s[:120] if s else fallback
