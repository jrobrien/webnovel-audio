"""The web player's data: the library as JSON, and who is listening where.

No login. A "profile" is a name someone picks in the browser; positions are
stored per profile and series so they follow the person across devices and
across the server's several addresses (browser storage cannot, being per-origin).
"""
from __future__ import annotations

import os
import re

from . import bundle
from .config import Config
from .db import DB
from .safepath import safe_slug

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._'-]{0,23}$")
MAX_T = 7 * 24 * 3600.0            # no chapter is longer than a week


def valid_profile_name(name) -> str | None:
    """The trimmed name, or None if it is not an acceptable profile name."""
    if not isinstance(name, str):
        return None
    name = re.sub(r"\s+", " ", name).strip()
    return name if _NAME_RE.match(name) else None


def library(cfg: Config, db: DB) -> dict:
    """Every series that has something to play, with its rendered chapters in order.

    Paused series are included on purpose: whether to *sync* a series and whether
    someone may want to listen to it are different questions.
    """
    out = []
    for s in db.list_series():
        slug = safe_slug(s["slug"], "")
        if not slug:
            continue
        chapters = []
        for c in db.chapters(s["id"]):
            if c["status"] != "rendered" or not c["audio_path"] \
                    or not os.path.exists(c["audio_path"]):
                continue
            chapters.append({
                "id": str(c["source_id"]),
                "n": c["ord"] + 1,
                "title": c["title"] or f"Chapter {c['ord'] + 1}",
                "dur": round(c["duration_s"] or 0),
                "audio": f"/audio/{slug}/{os.path.basename(c['audio_path'])}",
                "pub": c["published_at"] or "",
            })
        if not chapters:
            continue
        cover_local = os.path.exists(bundle.artifact(
            bundle.bundle_dir(cfg, s), "covers", "cover.jpg"))
        out.append({
            "slug": slug, "title": s["title"], "author": s["author"] or "",
            "status": s["status"] or "", "paused": not s["enabled"],
            "cover": f"/cover/{slug}.jpg" if cover_local else (s["cover_url"] or ""),
            "chapters": chapters,
        })
    out.sort(key=lambda x: x["title"].lower())
    return {"series": out}


def positions(db: DB, profile: str) -> dict:
    """{slug: {chapter, t, furthest, updated}} for one profile."""
    by_id = {s["id"]: safe_slug(s["slug"], "") for s in db.list_series()}
    return {"profile": profile, "series": {
        by_id[sid]: pos for sid, pos in db.get_positions(profile).items() if by_id.get(sid)}}


def save_position(db: DB, profile: str, slug: str, body) -> dict | None:
    """Validate and store one position update; None if the series/chapter is unknown."""
    if not isinstance(body, dict):
        return None
    chapter, furthest = body.get("chapter"), body.get("furthest")
    try:
        t = min(max(float(body.get("t") or 0), 0.0), MAX_T)
    except (TypeError, ValueError):
        return None
    if not isinstance(chapter, str) or not (furthest is None or isinstance(furthest, str)):
        return None
    s = next((r for r in db.list_series() if safe_slug(r["slug"], "") == slug), None)
    if not s:                       # exact slug only: get_series() also matches fragments
        return None
    return db.save_position(profile, s["id"], chapter, t, furthest)
