"""Per-series podcast RSS 2.0 (+ iTunes tags) built from the library DB."""
from __future__ import annotations

import datetime as _dt
import email.utils
import json
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


_SUPPORT_NAMES = {"patreon.com": "Patreon", "ko-fi.com": "Ko-fi", "paypal.me": "PayPal",
                  "paypal.com": "PayPal", "buymeacoffee.com": "Buy Me a Coffee",
                  "subscribestar.com": "SubscribeStar", "subscribestar.adult": "SubscribeStar"}


def _support_label(url: str) -> str:
    import urllib.parse
    host = urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")
    return _SUPPORT_NAMES.get(host, host or url)


def _credits(series) -> tuple[str, str]:
    """(plain, html) crediting the author: their profile and their own support
    pages. Empty when the series has neither."""
    author = _col(series, "author", "")
    profile = _col(series, "author_url", "")
    try:
        support = json.loads(_col(series, "support_urls", "") or "[]") or []
    except (TypeError, ValueError):
        support = []
    plain, html = [], []
    if author and profile:
        plain.append(f"Author: {author} — {profile}")
        html.append(f"Author: <a href={quoteattr(profile)}>{escape(author)}</a>")
    if support:
        plain.append("Support the author: " + " · ".join(
            f"{_support_label(u)} {u}" for u in support))
        html.append("Support the author: " + " · ".join(
            f"<a href={quoteattr(u)}>{escape(_support_label(u))}</a>" for u in support))
    return "\n".join(plain), (f"<p>{'<br/>'.join(html)}</p>" if html else "")


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
    # The positional number drifts from the author's numbering as soon as a volume
    # contains an interstitial (Sky Pride V2 has "Just wanted to say thank you" at
    # position 11, so every later chapter is one ahead of its own title), so it is
    # labelled "library #N" and never printed bare: the title keeps the author's
    # number, this one is what the UI, `state show` and `--range` use.
    by = f" · by {series['author']}" if _col(series, "author") else ""

    pub = str(_col(c, "published_at", ""))[:10]
    ren = str(_col(c, "rendered_at", ""))[:10]
    bits = []
    if pub:
        bits.append(f"published {pub}")
    if ren:
        bits.append(f"narrated {ren}")
    bits.append(f"library #{num}")
    if dur:
        bits.append(dur)
    line2 = " · ".join(bits)

    voice = _col(c, "narrator", "")
    line3 = f"Read by {voice} (Kokoro-82M), via webnovel-audio." if voice \
        else "Narrated by webnovel-audio (Kokoro-82M)."
    src_url = _col(c, "url", "")
    tail = "Personal-use narration of a free web serial. Not an official audiobook."

    cred_plain, cred_html = _credits(series)
    plain = "\n".join(filter(None, [
        title, where + by, line2, line3,
        f"Source: {src_url}" if src_url else "", cred_plain, tail]))
    link = (f"<p>Source: <a href={quoteattr(src_url)}>{escape(src_url)}</a></p>"
            if src_url else "")
    html = (f"<p><strong>{escape(title)}</strong><br/>{escape(where + by)}</p>"
            + (f"<p>{escape(line2)}</p>" if line2 else "")
            + f"<p>{escape(line3)}</p>" + link + cred_html
            + f"<p><em>{escape(tail)}</em></p>")
    return plain, html


def _guid_prefix(series) -> str:
    """GUIDs must never change once published, or podcast apps re-download
    every episode; the provider owns its prefix."""
    from . import providers
    return providers.get(_col(series, "provider", "")).guid


def _episode_notes(series, ep, vol, dur):
    """(plain, html) notes for a multi-chapter episode: the chapters it holds,
    each with its source link, then the same provenance line as a chapter."""
    where = series["title"] + (f" · {vol['title']}" if vol and vol.get("title") else "")
    by = f" · by {series['author']}" if _col(series, "author") else ""
    first, last = ep.first["ord"] + 1, ep.last["ord"] + 1
    bits = [f"library #{first}–{last}"] + ([dur] if dur else [])
    tail = "Personal-use narration of a free web serial. Not an official audiobook."
    lines = [ep.title, where + by, " · ".join(bits), ""]
    html = [f"<p><strong>{escape(ep.title)}</strong><br/>{escape(where + by)}</p>",
            f"<p>{escape(' · '.join(bits))}</p><ol>"]
    for c in ep.members:
        t = c["title"] or f"Chapter {c['ord'] + 1}"
        u = _col(c, "url", "")
        lines.append(f"{t}" + (f" — {u}" if u else ""))
        html.append(f"<li><a href={quoteattr(u)}>{escape(t)}</a></li>" if u
                    else f"<li>{escape(t)}</li>")
    cred_plain, cred_html = _credits(series)
    lines += ["", "Narrated by webnovel-audio (Kokoro-82M)."] + \
        ([cred_plain] if cred_plain else []) + [tail]
    html += ["</ol>", "<p>Narrated by webnovel-audio (Kokoro-82M).</p>", cred_html,
             f"<p><em>{escape(tail)}</em></p>"]
    return "\n".join(lines), "".join(html)


def build_feed(series, chapters, base_url: str, *, self_url: str = "",
               cover_local: bool = False, volumes: dict | None = None,
               episodes: list | None = None) -> str:
    """`episodes` (from `episodes.for_feed`): list those instead of one item per
    chapter."""
    volumes = volumes or {}
    base_url = base_url.rstrip("/")
    slug = series["slug"] or "series"

    if cover_local:
        cover = f"{base_url}/cover/{slug}.jpg"
    else:
        cover = series["cover_url"] or ""

    now = _dt.datetime.now(_dt.timezone.utc)
    guid = _guid_prefix(series)

    # one dict per feed item, oldest first, whichever unit the series uses
    entries: list[dict] = []
    if episodes is not None:
        for ep in episodes:
            vid = _col(ep.first, "volume_source_id")
            vol = volumes.get(vid) if vid else None
            dur = hms(ep.duration_s)
            plain, html = _episode_notes(series, ep, vol, dur)
            fname = os.path.basename(ep.path)
            entries.append({
                "title": ep.title, "path": ep.path, "dur": dur, "vol": vol,
                "url": f"{base_url}/audio/{slug}/episodes/{fname}",
                # the member count is in the id: a short tail episode that later
                # fills up is a different file, and must read as a new episode
                "guid": f"{guid}-{ep.first['source_id']}-x{len(ep.members)}",
                "published_at": ep.published_at, "episode": ep.index,
                "plain": plain, "html": html})
    else:
        rendered = [c for c in chapters
                    if c["status"] == "rendered" and c["audio_path"]
                    and os.path.exists(c["audio_path"])]
        rendered.sort(key=lambda c: c["ord"])
        for c in rendered:
            vid = _col(c, "volume_source_id")
            vol = volumes.get(vid) if vid else None
            fname = os.path.basename(c["audio_path"])
            dur = hms(c["duration_s"])
            num = c["ord"] + 1
            plain, html = _show_notes(series, c, vol, num, dur)
            entries.append({
                "title": c["title"] or f"Chapter {num}", "path": c["audio_path"],
                "dur": dur, "vol": vol, "url": f"{base_url}/audio/{slug}/{fname}",
                "guid": f"{guid}-{c['source_id']}", "published_at": c["published_at"],
                # Volume-relative episode number when we have one: the feed used to
                # say "chapter 69" for what the fiction calls Volume 2, Chapter 15.
                "episode": _col(c, "volume_chapter") or num,
                "plain": plain, "html": html})
    entries.reverse()                                     # newest first

    # Podcast apps (AntennaPod) only apply "new episode" actions to an item whose
    # pubDate is >= the feed's newest existing one, so a re-dated or inserted
    # non-chapter (cover/volume separator) would silence every chapter after it.
    # Cap each date at the earliest date of any later item: non-decreasing in order.
    ceiling = None
    for e in entries:                                     # newest first
        dt = _parse_iso(e["published_at"]) or now
        ceiling = dt if ceiling is None else min(ceiling, dt)
        e["pubdate"] = email.utils.format_datetime(ceiling)

    items = []
    for e in entries:
        vol, dur, fname = e["vol"], e["dur"], os.path.basename(e["path"])
        size = os.path.getsize(e["path"])
        items.append(
            "    <item>\n"
            f"      <title>{escape(e['title'])}</title>\n"
            f"      <guid isPermaLink=\"false\">{escape(e['guid'])}</guid>\n"
            f"      <pubDate>{e['pubdate']}</pubDate>\n"
            f"      <itunes:episode>{e['episode']}</itunes:episode>\n"
            + (f"      <itunes:season>{vol['index']}</itunes:season>\n" if vol else "")
            + f"      <itunes:title>{escape(e['title'])}</itunes:title>\n"
            + (f"      <itunes:duration>{dur}</itunes:duration>\n" if dur else "")
            + f"      <description>{escape(e['plain'])}</description>\n"
            + f"      <content:encoded><![CDATA[{e['html']}]]></content:encoded>\n"
            + f"      <itunes:summary>{escape(e['plain'])}</itunes:summary>\n"
            + f"      <enclosure url={quoteattr(e['url'])} length=\"{size}\" "
            f"type=\"{audio_mime(fname)}\"/>\n"
            "    </item>"
        )

    author = escape(series["author"] or "webnovel-audio")
    cred_plain, _ = _credits(series)
    channel_desc = f"{series['title']} — narrated by webnovel-audio." + \
        (f"\n{cred_plain}" if cred_plain else "")
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
        f"    <description>{escape(channel_desc)}</description>\n"
        "    <language>en</language>\n"
        f"    <lastBuildDate>{email.utils.format_datetime(now)}</lastBuildDate>\n"
        f"    <itunes:author>{author}</itunes:author>\n"
        "    <itunes:explicit>false</itunes:explicit>\n"
        '    <itunes:category text="Fiction"/>\n'
        f"{cover_block}{self_link}"
        + "\n".join(items) + ("\n" if items else "")
        + "  </channel>\n</rss>\n"
    )
