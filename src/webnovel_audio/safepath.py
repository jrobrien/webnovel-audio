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


_DECORATION_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")


def series_slug(title, fallback: str = "") -> str:
    """A short handle for a series from its title.

    Authors append promo text in brackets ("Sky Pride [Vol. 1 ... Read Now!]",
    "Railgun (LitRPG / OP MC) [Book 1 Completed]"), and sites bake all of it into
    their URL slug. Dropping bracketed/parenthesised text keeps the handle to the
    name itself. A title that is *only* decoration falls back to `fallback`.
    """
    bare = _DECORATION_RE.sub(" ", str(title or ""))
    slug = re.sub(r"[^a-z0-9]+", "-", bare.lower()).strip("-")
    return slug[:60].rstrip("-") or safe_slug(fallback)
