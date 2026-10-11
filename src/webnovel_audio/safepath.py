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
    bare = _DECORATION_RE.sub(" ", str(title or "")).replace("'", "").replace("\u2019", "")
    slug = re.sub(r"[^a-z0-9]+", "-", bare.lower()).strip("-")
    if len(slug) > SLUG_MAX:
        # cut between words, not mid-word: "…-drunken-monkey-s-journey-to-immor"
        slug = slug[:SLUG_MAX + 1].rsplit("-", 1)[0] if "-" in slug[:SLUG_MAX + 1] else slug[:SLUG_MAX]
    return slug.rstrip("-") or safe_slug(fallback)


SLUG_MAX = 40
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def valid_slug(slug: str) -> str | None:
    """Why `slug` can't be a series handle, or None if it can. Lower-case letters,
    digits and hyphens: it becomes a directory name and a URL segment."""
    if not slug:
        return "the slug is empty"
    if len(slug) > 60:
        return "the slug is longer than 60 characters"
    if not _SLUG_OK.match(slug) or slug.endswith("-") or "--" in slug:
        return "use lower-case letters, digits and single hyphens (e.g. drunken-monkey)"
    return None
