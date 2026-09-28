"""ScribbleHub: series page + paged table of contents -> chapter list,
chapter page -> Document.

How the site differs from a one-page chapter list:

  * The table of contents is paged: the series page shows the newest ~15
    chapters, `?toc=N` shows page N, newest first. Each entry is
    `<li class="toc_w" order="N">`, and `order` (1-based) is the reading
    order. The chapter id in the URL is not: ids are assigned when a chapter
    is created, so a glossary added later can sit at order 1 with a higher id.
  * `?toc=N` needs a `Referer` of the series page, or Cloudflare answers 403.
  * Chapter pages carry no OpenGraph tags; the body is `#chp_raw`.

Refresh is incremental: walk pages from the newest and stop at the first page
whose chapters all match what is already known (same id, same order). When
the site renumbers (an insertion shifts every order), no page matches and the
walk covers the whole list.
"""
from __future__ import annotations

import datetime as _dt
import math
import re

from bs4 import BeautifulSoup

from ..document import Document
from ..safepath import safe_slug
from . import html as H
from .base import ChapterRef, FetchContext, Provider, SeriesInfo
from .http import PoliteClient, fetch_asset

BASE = "https://www.scribblehub.com"
ASSET_HOSTS = ("scribblehub.com",)
DEFAULT_REQUEST_DELAY = 2.5

_SERIES_RE = re.compile(r"scribblehub\.com/series/(\d+)/([^/?#]+)")
_READ_RE = re.compile(r"scribblehub\.com/read/(\d+)-([^/?#]+)")
_CHAPTER_ID_RE = re.compile(r"/chapter/(\d+)")
_ID_RE = re.compile(r"^\d{1,12}$")


def series_url(source: str) -> tuple[str, str, str]:
    """(series id, slug, canonical series URL) from a series or chapter URL."""
    m = _SERIES_RE.search(source) or _READ_RE.search(source)
    if not m:
        raise SystemExit(f"not a ScribbleHub series or chapter URL: {source}")
    sid, slug = m.group(1), safe_slug(m.group(2), m.group(1))
    return sid, slug, f"{BASE}/series/{sid}/{slug}/"


def _title_slug(title: str, fallback: str) -> str:
    return safe_slug(re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80], fallback)


def _published(text: str, now: _dt.datetime | None = None) -> str:
    """'Sep 24, 2026 09:06 AM' or '17 hours ago' -> ISO timestamp, else ''."""
    text = (text or "").strip()
    try:
        return _dt.datetime.strptime(text, "%b %d, %Y %I:%M %p").strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        pass
    m = re.match(r"(\d+)\s+(second|minute|hour|day|week)s?\s+ago", text)
    if m:
        unit = {"second": "seconds", "minute": "minutes", "hour": "hours",
                "day": "days", "week": "weeks"}[m.group(2)]
        when = (now or _dt.datetime.now()) - _dt.timedelta(**{unit: int(m.group(1))})
        return when.strftime("%Y-%m-%dT%H:%M:%S")
    return ""


def parse_toc_page(html: str) -> list[ChapterRef]:
    """The chapters listed on one table-of-contents page."""
    soup = BeautifulSoup(html, "lxml")
    out: list[ChapterRef] = []
    for li in soup.select("li.toc_w[order]"):
        a = li.select_one("a.toc_a[href]")
        if a is None:
            continue
        m = _CHAPTER_ID_RE.search(a["href"])
        order = str(li.get("order") or "")
        if not (m and _ID_RE.match(m.group(1)) and order.isdigit() and int(order) >= 1):
            continue
        title = a.get_text(" ", strip=True)
        date = li.select_one("span.fic_date_pub")
        out.append(ChapterRef(
            source_id=m.group(1),
            order=int(order) - 1,
            title=title,
            slug=_title_slug(title, m.group(1)),
            url=a["href"] if a["href"].startswith("https://") else BASE + a["href"],
            published_at=_published(date.get("title", "") if date else ""),
        ))
    return out


def toc_page_count(html: str, total: int, per_page: int) -> int:
    m = re.search(r"pagination\(\{\s*items:\s*(\d+)", html)
    if m:
        return int(m.group(1))
    return max(1, math.ceil(total / per_page)) if per_page else 1


def _soft(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - metadata is decoration; never fatal
        return default


def parse_series_page(html: str, url: str) -> SeriesInfo:
    """Metadata from the series page (the chapter list is filled in separately)."""
    soup = BeautifulSoup(html, "lxml")
    sid, slug, canon = series_url(H.meta_content(soup, "og:url") or url)
    title = H.meta_content(soup, "og:title")
    if not title:
        t = soup.select_one("div.fic_title")
        title = t.get_text(" ", strip=True) if t else slug
    author = _soft(lambda: soup.select_one("span.auth_name_fic").get_text(strip=True), "")
    rating = _soft(lambda: round(float(re.search(r'"ratingValue":"([\d.]+)"', html)
                                        .group(1)), 2), 0.0)
    status = _soft(lambda: "ONGOING" if "Ongoing - " in html
                   else "COMPLETED" if "Completed - " in html
                   else "HIATUS" if "Hiatus - " in html else "", "")
    tags = _soft(lambda: [a.get_text(strip=True)
                          for a in soup.select("a.fic_genre, a.stag")][:40], [])
    return SeriesInfo(source_id=sid, slug=slug, title=title, author=author,
                      cover_url=H.meta_content(soup, "og:image"), url=canon,
                      provider="scribblehub", tags=tags, status=status, rating=rating)


def total_chapters(html: str) -> int:
    m = re.search(r'id="chpcounter"\s+value="(\d+)"', html)
    return int(m.group(1)) if m else 0


def merge_toc(pages, known: list[ChapterRef], total: int) -> list[ChapterRef] | None:
    """Combine the pages walked so far with what was already known.

    `pages` is an iterable of per-page chapter lists, newest page first; it is
    consumed only as far as needed. Returns the full list, or None when the
    walk ran out without the result adding up (the caller then walks it all).
    """
    by_id = {c.source_id: c for c in known}
    top = max((c.order for c in known), default=-1)
    seen: dict[str, ChapterRef] = {}
    for page in pages:
        if not page:
            break
        for c in page:
            seen[c.source_id] = c
        # every chapter on the page is either known at the same position or
        # new past the end, and the page overlaps what we know
        settled = any(c.source_id in by_id for c in page) and all(
            (c.source_id in by_id and by_id[c.source_id].order == c.order)
            or (c.source_id not in by_id and c.order > top) for c in page)
        if settled:
            merged = dict(by_id)
            merged.update(seen)
            out = sorted(merged.values(), key=lambda c: c.order)
            if len(out) == total and [c.order for c in out] == list(range(total)):
                return out
    out = sorted(seen.values(), key=lambda c: c.order)
    if total and len(out) == total and [c.order for c in out] == list(range(total)):
        return out
    return None


def parse_chapter(html: str, *, url: str = "") -> Document:
    soup = H.soup_of(html)
    content = soup.select_one("#chp_raw, div.chp_raw")
    if content is None:
        raise SystemExit("could not locate chapter content (#chp_raw) in the page")
    H.strip_hidden(content, H.hidden_class_names(soup))
    H.drop_matching(content, r"authornotes|modern-footnotes-footnote")
    # the site's editor wraps tables in `div.tbl_of`, and authors wrap runs of
    # blocks in bare divs; blocks are read from direct children, so unwrap
    for div in content.find_all("div"):
        div.unwrap()
    blocks = H.blocks_from_container(content)

    t = soup.select_one("div.chapter-title")
    byline = soup.select_one("div.chp_byauthor")
    fiction = byline.select_one('a[href*="/series/"]') if byline else None
    author = byline.select_one('a[href*="/profile/"]') if byline else None
    nxt = soup.select_one("a.btn-next[href]")
    canon = soup.find("link", rel="canonical")
    return Document(
        blocks=blocks,
        chapter_title=t.get_text(" ", strip=True) if t else "",
        fiction_title=fiction.get_text(" ", strip=True) if fiction else "",
        author=author.get_text(" ", strip=True) if author else "",
        url=url or (canon.get("href", "") if canon else ""),
        next_url=nxt["href"] if nxt else "",
    )


class ScribbleHubProvider(Provider):
    name = "scribblehub"
    guid_prefix = "sh"
    can_series = True

    def handles(self, source: str) -> bool:
        return source.startswith(("http://", "https://")) and "scribblehub.com" in source

    def recognizes(self, raw: str) -> bool:
        head = raw[:200000]
        return 'id="chp_raw"' in head and "scribblehub.com" in head

    def _get(self, url: str, ctx: FetchContext, *, referer: str = "") -> str:
        """The only network call. Tests replace this."""
        client = PoliteClient(
            delay=float(ctx.settings.get("request_delay", DEFAULT_REQUEST_DELAY)),
            label=self.name)
        try:
            return client.text(url, headers={"Referer": referer} if referer else None)
        finally:
            client.close()

    def series(self, source: str, ctx: FetchContext) -> SeriesInfo:
        _, _, canon = series_url(source)
        first = self._get(canon, ctx)
        info = parse_series_page(first, canon)
        total = total_chapters(first)
        page1 = parse_toc_page(first)
        pages = toc_page_count(first, total, len(page1))

        def walk():
            yield page1
            for n in range(2, pages + 1):
                yield parse_toc_page(self._get(f"{canon}?toc={n}", ctx, referer=canon))

        chapters = merge_toc(walk(), ctx.known, total)
        if chapters is None:
            raise SystemExit(f"scribblehub: table of contents for {canon} did not add "
                             f"up to its {total} chapters")
        info.chapters = chapters
        return info

    def fetch(self, ref: ChapterRef, ctx: FetchContext) -> str:
        m = _READ_RE.search(ref.url)
        referer = f"{BASE}/series/{m.group(1)}/{m.group(2)}/" if m else ""
        return self._get(ref.url, ctx, referer=referer)

    def parse(self, raw: str, ref: ChapterRef, ctx: FetchContext) -> Document:
        doc = parse_chapter(raw)
        doc.url = doc.url or ref.url
        return doc

    def fetch_cover(self, url: str, ctx: FetchContext) -> bytes | None:
        return fetch_asset(url, ASSET_HOSTS)
