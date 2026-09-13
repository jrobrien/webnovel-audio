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


def _col(row, name, default=None):
    """Read a column from a sqlite3.Row or a plain dict, tolerating absence.

    Feed building runs against rows from callers that may predate a column, and
    against hand-built dicts in tests; a missing key is not an error here.
    """
    try:
        keys = row.keys()
    except AttributeError:
        return default
    if name not in keys:
        return default
    v = row[name]
    return default if v is None else v


def _show_notes(series, c, vol, num, dur):
    """(plain, html) per-episode notes.

    Carries the same provenance the .opus already embeds — source link, when it
    was published, when we narrated it, and with what. Deliberately no synopsis:
    that's the author's prose, and the source link stands in for it.
    """
    title = c["title"] or f"Chapter {num}"
    where = series["title"]
    if vol and vol.get("title"):
        where += f" · {vol['title']}"
    # Deliberately NOT printing the positional number here. It drifts from the
    # author's numbering as soon as a volume contains an interstitial — Sky
    # Pride V2 has "Just wanted to say thank you" at position 11, so every
    # later chapter is one ahead of its own title. The title already carries
    # the author's number; contradicting it would be worse than omitting it.
    by = f" · by {series['author']}" if _col(series, "author") else ""

    pub = str(_col(c, "published_at", ""))[:10]
    ren = str(_col(c, "rendered_at", ""))[:10]
    bits = []
    if pub:
        bits.append(f"published {pub}")
    if ren:
        bits.append(f"narrated {ren}")
    if dur:
        bits.append(dur)
    line2 = " · ".join(bits)

    voice = _col(c, "narrator", "")
    line3 = f"Read by {voice} (Kokoro-82M), via webnovel-audio." if voice \
        else "Narrated by webnovel-audio (Kokoro-82M)."
    src_url = _col(c, "url", "")
    tail = "Personal-use narration of a free web serial. Not an official audiobook."

    plain = "\n".join(filter(None, [
        title, where + by, line2, line3,
        f"Source: {src_url}" if src_url else "", tail]))
    link = (f"<p>Source: <a href={quoteattr(src_url)}>{escape(src_url)}</a></p>"
            if src_url else "")
    html = (f"<p><strong>{escape(title)}</strong><br/>{escape(where + by)}</p>"
            + (f"<p>{escape(line2)}</p>" if line2 else "")
            + f"<p>{escape(line3)}</p>" + link
            + f"<p><em>{escape(tail)}</em></p>")
    return plain, html


def build_feed(series, chapters, base_url: str, *, self_url: str = "",
               cover_local: bool = False, volumes: dict | None = None) -> str:
    volumes = volumes or {}
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
        vid = _col(c, "volume_rr_id")
        vol = volumes.get(vid) if vid else None
        fname = os.path.basename(c["audio_path"])
        size = os.path.getsize(c["audio_path"])
        url = f"{base_url}/audio/{slug}/{fname}"
        dur = hms(c["duration_s"])
        num = c["ord"] + 1
        title = c["title"] or f"Chapter {num}"
        # Volume-relative episode number when we have one: the feed used to say
        # "chapter 69" for what the fiction calls Volume 2, Chapter 15.
        episode = _col(c, "volume_chapter") or num
        plain, html = _show_notes(series, c, vol, num, dur)
        items.append(
            "    <item>\n"
            f"      <title>{escape(title)}</title>\n"
            f"      <guid isPermaLink=\"false\">rr-{escape(str(c['rr_id']))}</guid>\n"
            f"      <pubDate>{rfc822(c['published_at'], now)}</pubDate>\n"
            f"      <itunes:episode>{episode}</itunes:episode>\n"
            + (f"      <itunes:season>{vol['index']}</itunes:season>\n" if vol else "")
            + f"      <itunes:title>{escape(title)}</itunes:title>\n"
            + (f"      <itunes:duration>{dur}</itunes:duration>\n" if dur else "")
            + f"      <description>{escape(plain)}</description>\n"
            + f"      <content:encoded><![CDATA[{html}]]></content:encoded>\n"
            + f"      <itunes:summary>{escape(plain)}</itunes:summary>\n"
            + f"      <enclosure url={quoteattr(url)} length=\"{size}\" "
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
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
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
