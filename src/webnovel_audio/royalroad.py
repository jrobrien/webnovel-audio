"""Royal Road fetch + fiction/chapter-list parsing, with optional cookie auth.

The chapter list on a fiction page is authoritative in a `window.chapters = [...]`
script (complete, ordered, with lock state); the visible <table> is only a page of
it and is used as a fallback.

Auth is a stored session cookie and nothing more: `login` parses cookies out of a
browser export and saves them; `RRClient` attaches them to content fetches so a
subscriber-only chapter can be read. It is deliberately never *verified* up
front — that needs an account page whose markup drifts, and a stale cookie shows
up honestly at the point of use instead (a locked chapter fails to fetch).
Nothing here reads account state.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

BASE = "https://www.royalroad.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)
CONFIG_DIR = os.path.expanduser("~/.config/webnovel-audio")
SESSION_FILE = os.path.join(CONFIG_DIR, "session.json")


@dataclass
class ChapterRef:
    rr_id: str
    order: int
    title: str
    slug: str
    url: str
    published_at: str = ""
    unlocked: bool = True
    volume_id: str = ""       # provider volumeId; "" is normal (many chapters
                              # are in no volume at all)


@dataclass
class VolumeRef:
    rr_id: str
    title: str
    cover_url: str = ""
    order: int = 0


@dataclass
class FictionInfo:
    rr_id: str
    slug: str
    title: str
    author: str = ""
    author_url: str = ""
    cover_url: str = ""
    url: str = ""
    chapters: list[ChapterRef] = field(default_factory=list)
    volumes: list[VolumeRef] = field(default_factory=list)
    provider: str = "royalroad"
    tags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)   # "Graphic Violence", ...
    status: str = ""                                    # ONGOING | COMPLETED | ...
    rating: float = 0.0


# --- session cookie ------------------------------------------------------------

def load_session() -> dict:
    try:
        with open(SESSION_FILE, encoding="utf-8") as fh:
            return json.load(fh).get("cookies", {})
    except (OSError, json.JSONDecodeError):
        return {}


def save_session(cookies: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SESSION_FILE, "w", encoding="utf-8") as fh:
        json.dump({"cookies": cookies}, fh)
    os.chmod(SESSION_FILE, 0o600)


def clear_session() -> None:
    try:
        os.remove(SESSION_FILE)
    except OSError:
        pass


def parse_cookie_header(s: str) -> dict:
    out: dict[str, str] = {}
    for part in s.strip().strip(";").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_cookies_txt(path: str) -> dict:
    """Netscape cookies.txt -> {name: value} for royalroad.com."""
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) >= 7 and "royalroad.com" in cols[0]:
                out[cols[5]] = cols[6]
    return out


# --- unauthenticated asset fetch --------------------------------------------

_ASSET_HOSTS = ("royalroad.com", "royalroadcdn.com")


def fetch_asset(url: str, *, timeout: float = 15.0) -> bytes | None:
    """GET a cover/asset URL **without** the session cookie, and only from a Royal
    Road host. Covers come from an attacker-controlled `og:image`; using the
    logged-in client would leak the session cookie to an arbitrary server.
    """
    import httpx

    try:
        host = (httpx.URL(url).host or "").lower()
    except Exception:  # noqa: BLE001
        return None
    if not any(host == h or host.endswith("." + h) for h in _ASSET_HOSTS):
        return None
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT},
                      follow_redirects=True, timeout=timeout)
        r.raise_for_status()
        return r.content
    except httpx.HTTPError:
        return None


# --- client ------------------------------------------------------------------

class RRClient:
    # Politeness is a property of us-vs-the-server, not of a client object, and
    # `providers.RoyalRoadProvider.raw` builds a fresh client per chapter — a
    # per-instance timestamp meant `fetch <slug> 1-50` fired 50 requests with no
    # gap at all. Keep it on the class so the spacing survives client churn.
    _last_request = 0.0
    _rate_lock = threading.Lock()

    def __init__(self, delay: float = 2.5, cookies: dict | None = None):
        self.delay = delay
        self._c = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            cookies=cookies if cookies is not None else load_session(),
            follow_redirects=True,
            timeout=30.0,
        )

    def _abs(self, url: str) -> str:
        return url if url.startswith("http") else BASE + url

    def _wait_turn(self) -> None:
        """Space requests `delay` apart across every client in this process."""
        with RRClient._rate_lock:
            gap = time.monotonic() - RRClient._last_request
            if gap < self.delay:
                time.sleep(self.delay - gap)
            RRClient._last_request = time.monotonic()

    def get(self, url: str, *, tries: int = 4) -> httpx.Response:
        self._wait_turn()
        last_exc: Exception | None = None
        for attempt in range(tries):
            try:
                resp = self._c.get(self._abs(url))
                RRClient._last_request = time.monotonic()
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(self.delay * (attempt + 2))
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:  # pragma: no cover - network
                last_exc = exc
                time.sleep(self.delay * (attempt + 2))
        raise SystemExit(f"royalroad: giving up on {url} ({last_exc})")

    def html(self, url: str) -> str:
        return self.get(url).text

    def close(self) -> None:
        self._c.close()


# --- parsing ---------------------------------------------------------------

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
_SLUG_OK_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_id(value) -> str:
    """A Royal Road numeric id, or '' if it isn't one. Never used raw in a path."""
    s = str(value or "").strip()
    return s if _ID_RE.match(s) else ""


def _safe_slug(value, fallback: str = "") -> str:
    """A path/URL-safe slug: keep [A-Za-z0-9._-], drop the rest, no leading dots."""
    s = _SLUG_OK_RE.sub("-", str(value or "").strip()).strip(".-")
    return s[:120] if s else fallback


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
        return _safe_id(m.group(1)), _safe_slug(m.group(2))
    m = re.search(r"/fiction/(\d+)", src or fallback_url)
    return (_safe_id(m.group(1)) if m else ""), ""


_STATUS_LABELS = {"ONGOING", "COMPLETED", "HIATUS", "STUB", "DROPPED"}
# Presentational classes Royal Road can rename at will. Every extractor below is
# wrapped so that drift degrades to an empty value — losing a tag list must never
# break a sync, and an empty list is a truthful "we don't know".
_TAG_SEL = "a.fiction-tag"
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
            out.append(VolumeRef(rr_id=vid, title=str(v.get("title", "")).strip(),
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


def parse_fiction(html: str, url: str = "") -> FictionInfo:
    soup = BeautifulSoup(html, "lxml")
    rr_id, slug = _fiction_id_slug(soup, url)
    slug = slug or (_safe_slug(rr_id, "fiction") if rr_id else "fiction")

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
                cslug = _safe_slug(c.get("slug"), cid)
                chapters.append(ChapterRef(
                    rr_id=cid,
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
                rr_id=cid,
                order=i,
                title=link.get_text(strip=True) if link else "",
                slug=_safe_slug(m.group(2), cid),
                url=BASE + href if href.startswith("/") else href,
                published_at=(tm.get("datetime") if tm else "") or "",
            ))

    chapters.sort(key=lambda c: c.order)
    return FictionInfo(
        rr_id=rr_id, slug=slug, title=title, author=author, author_url=author_url,
        cover_url=cover, url=url or f"{BASE}/fiction/{rr_id}/{slug}", chapters=chapters,
        tags=_parse_tags(soup), warnings=_parse_warnings(soup),
        status=_parse_status(soup), rating=_parse_rating(soup),
        volumes=_parse_volumes(html),
    )
