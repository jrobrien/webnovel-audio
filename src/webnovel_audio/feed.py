"""Per-series podcast RSS 2.0 (+ iTunes tags) built from the library DB."""
from __future__ import annotations

import datetime as _dt
import email.utils
import os
import re
from xml.sax.saxutils import escape, quoteattr

_AUDIO_TYPES = {".opus": "audio/ogg", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
                ".m4b": "audio/mp4", ".mp3": "audio/mpeg"}


def audio_mime(path: str) -> str:
    return _AUDIO_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def _parse_iso(s: str) -> _dt.datetime | None:
    if not s:
        return None
    s = re.sub(r"\.\d+", "", s.strip().replace("Z", "+00:00"))  # drop fractional secs
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = _dt.datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            continue
    try:
        dt = _dt.datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


def rfc822(iso: str, fallback: _dt.datetime | None = None) -> str:
    dt = _parse_iso(iso) or fallback or _dt.datetime.now(_dt.timezone.utc)
    return email.utils.format_datetime(dt)


def hms(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def build_feed(series, chapters, base_url: str, *, self_url: str = "",
               cover_local: bool = False) -> str:
    base_url = base_url.rstrip("/")
    slug = series["slug"] or "series"
    rendered = [c for c in chapters
                if c["status"] == "rendered" and c["audio_path"]
                and os.path.exists(c["audio_path"])]
    rendered.sort(key=lambda c: c["ord"], reverse=True)   # newest first

    if cover_local:
        cover = f"{base_url}/cover/{slug}.jpg"
    else:
        cover = series["cover_url"] or ""

    now = _dt.datetime.now(_dt.timezone.utc)
    items = []
    for c in rendered:
        fname = os.path.basename(c["audio_path"])
        size = os.path.getsize(c["audio_path"])
        url = f"{base_url}/audio/{slug}/{fname}"
        dur = hms(c["duration_s"])
        num = c["ord"] + 1
        title = c["title"] or f"Chapter {num}"
        desc = f"{series['title']} — chapter {num}"
        items.append(
            "    <item>\n"
            f"      <title>{escape(title)}</title>\n"
            f"      <guid isPermaLink=\"false\">rr-{escape(str(c['rr_id']))}</guid>\n"
            f"      <pubDate>{rfc822(c['published_at'], now)}</pubDate>\n"
            f"      <itunes:episode>{num}</itunes:episode>\n"
            f"      <itunes:title>{escape(title)}</itunes:title>\n"
            + (f"      <itunes:duration>{dur}</itunes:duration>\n" if dur else "")
            + f"      <description>{escape(desc)}</description>\n"
            f"      <enclosure url={quoteattr(url)} length=\"{size}\" "
            f"type=\"{audio_mime(fname)}\"/>\n"
            "    </item>"
        )

    author = escape(series["author"] or "webnovel-audio")
    self_link = (
        f'    <atom:link href={quoteattr(self_url)} rel="self" type="application/rss+xml"/>\n'
        if self_url else ""
    )
    cover_block = (
        f"    <itunes:image href={quoteattr(cover)}/>\n"
        f"    <image><url>{escape(cover)}</url><title>{escape(series['title'])}</title>"
        f"<link>{escape(series['url'] or base_url)}</link></image>\n"
        if cover else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{escape(series['title'])}</title>\n"
        f"    <link>{escape(series['url'] or base_url)}</link>\n"
        f"    <description>{escape(series['title'])} — narrated by webnovel-audio.</description>\n"
        "    <language>en</language>\n"
        f"    <lastBuildDate>{email.utils.format_datetime(now)}</lastBuildDate>\n"
        f"    <itunes:author>{author}</itunes:author>\n"
        "    <itunes:explicit>false</itunes:explicit>\n"
        '    <itunes:category text="Fiction"/>\n'
        f"{cover_block}{self_link}"
        + "\n".join(items) + ("\n" if items else "")
        + "  </channel>\n</rss>\n"
    )
