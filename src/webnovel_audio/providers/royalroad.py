"""Royal Road: fiction page -> chapter list, chapter page -> Document.

The chapter list on a fiction page is authoritative in a `window.chapters = [...]`
script (complete, ordered, with lock state); the visible <table> is only a page of
it and is used as a fallback.

Royal Road injects anti-piracy decoy text into chapter bodies: a `<style>` rule
like `.<random>{display:none}` plus a paragraph with that class. The shared
`html.strip_hidden` removes it.

Royal Road is `browser_only`: it answers plain HTTP with a bot check (Anubis,
Cloudflare), so no page is fetched here. A browser extension sends the pages
(see `grab.py`, `sync.ingest_raw`), and a logged-in browser covers locked
chapters, so there is no stored login either.
"""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from ..document import Document
from ..safepath import safe_slug
from . import html as H
from .base import BrowserOnly, ChapterLocked, ChapterRef, FetchContext, Provider, SeriesInfo, VolumeRef
from .http import fetch_asset

BASE = "https://www.royalroad.com"
ASSET_HOSTS = ("royalroad.com", "royalroadcdn.com")


# --- parsing: fiction page ---------------------------------------------------

def _extract_js_array(html: str, name: str) -> str | None:
    m = re.search(rf"window\.{re.escape(name)}\s*=\s*", html)
    if not m or not html[m.end():m.end() + 1] == "[":
        return None
    s = html[m.end():]
    depth = 0
    in_str = esc = False
    for k, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return s[:k + 1]
    return None


_ID_RE = re.compile(r"^\d{1,12}$")


def _safe_id(value) -> str:
    """A Royal Road numeric id, or '' if it isn't one."""
    s = str(value or "").strip()
    return s if _ID_RE.match(s) else ""


def _fiction_id_slug(soup: BeautifulSoup, fallback_url: str) -> tuple[str, str]:
    src = ""
    og = soup.find("meta", property="og:url")
    if og and og.get("content"):
        src = og["content"]
    if "/fiction/" not in src:
        a = soup.select_one('a[href*="/fiction/"][href*="/chapter/"]')
        src = a["href"] if a else fallback_url
    m = re.search(r"/fiction/(\d+)/([^/?#]+)", src)
    if m:
        return _safe_id(m.group(1)), safe_slug(m.group(2))
    m = re.search(r"/fiction/(\d+)", src or fallback_url)
    return (_safe_id(m.group(1)) if m else ""), ""


_STATUS_LABELS = {"ONGOING", "COMPLETED", "HIATUS", "STUB", "DROPPED"}
# Presentational classes Royal Road can rename at will. Every extractor below is
# wrapped so that drift degrades to an empty value — losing a tag list must never
# break a sync, and an empty list is a truthful "we don't know".
_TAG_SEL = 'a.fiction-tag, a[href*="tagsAdd="]'   # the second is the browser-saved layout
_WARN_SEL = ".text-center.font-red-sunglo li"


def _soft(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - metadata is decoration; never fatal
        return default


def _parse_volumes(html: str) -> list[VolumeRef]:
    """`window.volumes` -> VolumeRef list.

    Authoritative JSON, same as `window.chapters`. Two things that bite a naive
    reader, both seen in the wild: `order` is NOT contiguous (Sky Pride runs
    1,2,3,4,6,7), and volumes are optional per chapter — Spector leaves 529 of
    746 chapters unassigned, and plenty of fictions have no volumes at all.
    """
    def go():
        raw = _extract_js_array(html, "volumes")
        if not raw:
            return []
        out = []
        for v in json.loads(raw):
            vid = _safe_id(v.get("id"))
            if not vid:
                continue
            cover = str(v.get("cover") or "")
            out.append(VolumeRef(source_id=vid, title=str(v.get("title", "")).strip(),
                                 cover_url=cover, order=int(v.get("order") or 0)))
        return sorted(out, key=lambda v: v.order)
    return _soft(go, [])


def _parse_tags(soup) -> list[str]:
    return _soft(lambda: [t for t in (a.get_text(strip=True)
                                      for a in soup.select(_TAG_SEL)) if t][:40], [])


def _parse_warnings(soup) -> list[str]:
    return _soft(lambda: [t for t in (li.get_text(strip=True)
                                      for li in soup.select(_WARN_SEL)) if t][:20], [])


def _parse_status(soup) -> str:
    def go():
        for sp in soup.select("span.label"):
            t = sp.get_text(strip=True).upper()
            if t in _STATUS_LABELS:
                return t
        return ""
    return _soft(go, "")


def _parse_rating(soup) -> float:
    def go():
        m = soup.find("meta", property="books:rating:value")
        return round(float(m["content"]), 2) if m and m.get("content") else 0.0
    return _soft(go, 0.0)


def _jsonld_author(soup) -> tuple[str, str]:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            a = item.get("author") if isinstance(item, dict) else None
            if isinstance(a, dict) and a.get("name"):
                return str(a["name"]).strip(), str(a.get("url") or "")
    return "", ""


def parse_fiction(html: str, url: str = "") -> SeriesInfo:
    soup = BeautifulSoup(html, "lxml")
    rr_id, slug = _fiction_id_slug(soup, url)
    slug = slug or (safe_slug(rr_id, "fiction") if rr_id else "fiction")

    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].split(" | ")[0].strip()
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""

    author = author_url = ""
    a = soup.select_one('h4 a[href^="/profile/"], a[href^="/profile/"]')
    if a:
        author, author_url = a.get_text(strip=True), BASE + a["href"]
    if not author:      # the redesigned page links absolute URLs; its JSON-LD still names the author
        author, author_url = _jsonld_author(soup)
    cover = ""
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        cover = og_img["content"]

    chapters: list[ChapterRef] = []
    raw = _extract_js_array(html, "chapters")
    if raw:
        try:
            for c in json.loads(raw):
                cid = _safe_id(c.get("id"))
                if not cid:                    # no usable id -> can't address it safely
                    continue
                cslug = safe_slug(c.get("slug"), cid)
                chapters.append(ChapterRef(
                    source_id=cid,
                    order=int(c.get("order", len(chapters))),
                    title=str(c.get("title", "")).strip(),
                    slug=cslug,
                    url=f"{BASE}/fiction/{rr_id}/{slug}/chapter/{cid}/{cslug}",
                    published_at=str(c.get("date", "")),
                    unlocked=bool(c.get("isUnlocked", True)),
                    volume_id=_safe_id(c.get("volumeId")),
                ))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            chapters = []

    if not chapters:  # fallback: the visible table
        for i, tr in enumerate(soup.select("#chapters tbody tr, table#chapters tbody tr")):
            href = tr.get("data-url") or (tr.find("a", href=True) or {}).get("href", "")
            if not href:
                continue
            tm = tr.find("time")
            link = tr.find("a", href=True)
            m = re.search(r"/chapter/(\d+)/([^/?#]+)", href)
            cid = _safe_id(m.group(1)) if m else ""
            if not cid:
                continue
            chapters.append(ChapterRef(
                source_id=cid,
                order=i,
                title=link.get_text(strip=True) if link else "",
                slug=safe_slug(m.group(2), cid),
                url=BASE + href if href.startswith("/") else href,
                published_at=(tm.get("datetime") if tm else "") or "",
            ))

    chapters.sort(key=lambda c: c.order)
    return SeriesInfo(
        source_id=rr_id, slug=slug, title=title, author=author, author_url=author_url,
        cover_url=cover, url=url or f"{BASE}/fiction/{rr_id}/{slug}", chapters=chapters,
        tags=_parse_tags(soup), warnings=_parse_warnings(soup),
        status=_parse_status(soup), rating=_parse_rating(soup),
        volumes=_parse_volumes(html), provider="royalroad",
    )


# --- parsing: chapter page -------------------------------------------------

def _chapter_container(soup: BeautifulSoup):
    for sel in ("div.chapter-content", "div.chapter-inner", "div.chapter-page"):
        node = soup.select_one(sel)
        if node:
            return node
    return H.guess_content_container(soup)


def _split_og_title(og_title: str, soup: BeautifulSoup) -> tuple[str, str]:
    """`og:title` is "<chapter title> - <fiction>", but a chapter title can hold
    " - " itself ("Chapter 17 - Plans Manifest(?) (III) - Cultivation System"), so
    splitting on the first one hands the series a title that is half a chapter's.
    The page's own `<h1>` is the chapter title; whatever follows it is the fiction.
    Without an h1 to go by, the *last* " - " is the safer split."""
    h1 = soup.select_one("h1.font-white")
    own = h1.get_text(" ", strip=True) if h1 else ""
    if own and og_title.startswith(own + " - "):
        return own, og_title[len(own) + 3:].strip()
    if " - " in og_title:          # "4. Deck Four - Salvage Run"
        left, right = og_title.rsplit(" - ", 1)
        return left.strip(), right.strip()
    return og_title, ""


def parse_chapter(html: str, *, url: str = "") -> Document:
    soup = H.soup_of(html)
    content = _chapter_container(soup)
    if content is None:
        raise SystemExit("could not locate chapter content in the page")

    H.strip_hidden(content, H.hidden_class_names(soup))
    H.drop_matching(content, "author-note")
    blocks = H.blocks_from_container(content)

    og_title = H.meta_content(soup, "og:title", "twitter:title")
    chapter_title, fiction_title = _split_og_title(og_title, soup)
    if not chapter_title:
        h1 = soup.find("h1")
        chapter_title = h1.get_text(" ", strip=True) if h1 else ""
    blocks = H.drop_title_echo(blocks, chapter_title)

    a = soup.select_one('.fic-header a[href*="/profile/"], a[href*="/profile/"]')
    return Document(
        blocks=blocks,
        fiction_title=fiction_title,
        chapter_title=chapter_title,
        author=a.get_text(" ", strip=True) if a else "",
        url=url or H.meta_content(soup, "og:url"),
        next_url=H.next_link_by_text(soup),
    )


# --- the provider ------------------------------------------------------------

_OG_URL_RE = re.compile(r"""<meta[^>]+og:url[^>]+royalroad\.com""", re.I)
_FICTION_URL_RE = re.compile(
    r"""<meta[^>]+og:url[^>]+royalroad\.com/fiction/(\d+)(?:/[^/"']*)?["']""", re.I)
_CHAPTER_URL_RE = re.compile(
    r"""<meta[^>]+og:url[^>]+royalroad\.com/fiction/(\d+)/[^/"']+/chapter/(\d+)""", re.I)


class RoyalRoadProvider(Provider):
    name = "royalroad"
    guid_prefix = "rr"          # published feeds use rr-<id>; never change it
    can_series = True
    browser_only = True         # Anubis + Cloudflare: pages come from a browser

    def handles(self, source: str) -> bool:
        return source.startswith(("http://", "https://")) and "royalroad.com" in source

    def recognizes(self, raw: str) -> bool:
        return bool(_OG_URL_RE.search(raw[:20000]))

    def identify(self, raw: str) -> tuple[str, str] | None:
        m = _CHAPTER_URL_RE.search(raw[:20000])
        return (m.group(1), m.group(2)) if m else None

    def identify_series(self, raw: str) -> str | None:
        m = _FICTION_URL_RE.search(raw[:20000])
        return m.group(1) if m else None

    def reading_position(self, raw: str) -> str | None:
        """The chapter the logged-in reader is up to, as the series page records it."""
        m = re.search(r"window\.readingProgress\s*=\s*(\d+)", raw)
        return m.group(1) if m else None

    def _get(self, url: str, ctx: FetchContext) -> str:
        """The only network call, and it is not made: Royal Road answers plain HTTP
        with a bot check. Pages arrive from a browser (`grab`, `ingest`); tests
        replace this to feed fixtures."""
        raise BrowserOnly("Royal Road")

    def series(self, source: str, ctx: FetchContext) -> SeriesInfo:
        return parse_fiction(self._get(source, ctx), url=source)

    def parse_series_page(self, raw: str, url: str) -> SeriesInfo:
        return parse_fiction(raw, url=url)

    def check_fetchable(self, ref: ChapterRef, ctx: FetchContext) -> None:
        if ref.unlocked:
            return
        raise ChapterLocked("chapter is marked locked; read it in the browser while "
                            "logged in to the site, then it is saved like any other")

    def fetch(self, ref: ChapterRef, ctx: FetchContext) -> str:
        return self._get(ref.url, ctx)

    def parse(self, raw: str, ref: ChapterRef, ctx: FetchContext) -> Document:
        doc = parse_chapter(raw)
        doc.url = doc.url or ref.url
        return doc

    def fetch_cover(self, url: str, ctx: FetchContext) -> bytes | None:
        return fetch_asset(url, ASSET_HOSTS)
