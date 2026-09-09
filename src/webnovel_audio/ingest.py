"""HTML ingest: a chapter web page (or saved .html) -> Blocks + metadata.

Tuned for Royal Road, but the shape is generic. Two jobs matter here:

  1. Strip Royal Road's anti-piracy decoy text. RR injects a `<style>` rule like
     `.<random>{display:none}` and drops a paragraph/span with that class into the
     chapter body ("Unauthorized content usage: ..."). We parse the stylesheets,
     collect every selector that hides content, and decompose matching nodes
     (plus inline `display:none`, `hidden`, `aria-hidden`) before reading text.

  2. Preserve italics. Whole-sentence / whole-paragraph italics in this genre are
     internal monologue; `segment.build_segments` routes those to the `thought`
     voice. We record italic character ranges on the raw block text so the
     segmenter can measure per-sentence coverage.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

from .normalize import Block

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

_ITALIC_TAGS = {"em", "i"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SYSTEM_TAGS = {"blockquote", "table", "pre"}

_HIDE_RULE_RE = re.compile(
    r"([^{}]+)\{[^{}]*?(?:display\s*:\s*none|visibility\s*:\s*hidden|speak\s*:\s*never)[^{}]*?\}",
    re.IGNORECASE | re.DOTALL,
)
_CLASS_IN_SELECTOR_RE = re.compile(r"\.([A-Za-z0-9_-]+)")
_BREAK_GLYPHS_RE = re.compile(r"^[\s*#~•·✦❖◆○◇⁂=_+.\-–—]{2,}$")
_SYSTEM_HINT_RE = re.compile(
    r"\b(HP|MP|SP|XP|EXP|STR|DEX|CON|INT|WIS|CHA|VIT|AGI|LUK)\b\s*[:=]"
    r"|\b(Level|Lvl|Rank|Class|Race|Title|Skill|Quest)\b\s*[:=]"
    r"|\b\d+\s*/\s*\d+\b",
    re.IGNORECASE,
)

# Livestream "chat" line: [Handle (Location): message]  /  [Handle: message]
_CHAT_LOC_RE = re.compile(r"^\s*(.{2,40}?)\s*\(\s*([^)]{1,30}?)\s*\)\s*:\s*(.+)$", re.S)
_CHAT_BARE_RE = re.compile(r"^\s*([^:()\[\]]{2,40}?)\s*:\s*(.+)$", re.S)
_SYSTEM_KEYWORDS = {
    "target", "objective", "quest", "mission", "system", "warning", "alert",
    "error", "notice", "status", "update", "analysis", "scan", "scanning",
    "affirmative", "negative", "ping", "alarm", "alert", "note", "task",
}
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. ]*$")


def _parse_chat(text: str) -> dict | None:
    """Return {user, location, message} for a chat line, else None."""
    if not (text.startswith("[") and text.rstrip().endswith("]")):
        return None
    inner = text.strip().lstrip("[").rstrip("]").strip()
    m = _CHAT_LOC_RE.match(inner)
    if m:
        user, location, message = m.group(1), m.group(2), m.group(3)
    else:
        m = _CHAT_BARE_RE.match(inner)
        if not m:
            return None
        user, location, message = m.group(1), "", m.group(2)
    user = user.strip().rstrip(":").strip()
    if not user or len(user) > 40 or not _HANDLE_RE.match(user):
        return None
    if not location and user.lower() in _SYSTEM_KEYWORDS:
        return None  # [Target: ...] etc. -> a system line, not chat
    if not location and " " in user:
        return None  # a sentence, not a handle
    return {"user": user, "location": location.strip(), "message": message.strip()}


@dataclass
class Document:
    blocks: list[Block]
    fiction_title: str = ""
    chapter_title: str = ""
    author: str = ""
    url: str = ""
    next_url: str = ""
    meta: dict = field(default_factory=dict)
    # provenance, filled in by pipeline.load_document
    raw_sha256: str = ""
    raw_bytes: int = 0
    retrieved_at: str = ""


def fetch_html(url: str, *, timeout: float = 30.0) -> str:
    """One polite GET. For bulk/authenticated fetching see the Phase 4 sync layer."""
    import httpx

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _hidden_class_names(soup: BeautifulSoup) -> set[str]:
    hidden: set[str] = set()
    for style in soup.find_all("style"):
        css = style.string or style.get_text() or ""
        for match in _HIDE_RULE_RE.finditer(css):
            for selector in match.group(1).split(","):
                hidden.update(_CLASS_IN_SELECTOR_RE.findall(selector))
    return hidden


def _strip_hidden(container: Tag, hidden_classes: set[str]) -> None:
    # executable / non-prose elements never contribute story text
    for el in list(container.find_all(
        ["script", "style", "noscript", "template", "iframe", "object", "embed", "svg"]
    )):
        el.decompose()
    for el in list(container.find_all(class_=True)):
        classes = el.get("class") or []
        if any(c in hidden_classes for c in classes):
            el.decompose()
    for el in list(container.select('[hidden], [aria-hidden="true"]')):
        el.decompose()
    for el in list(container.find_all(style=True)):
        style = (el.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            el.decompose()
    for el in list(container.find_all(class_=re.compile("author-note", re.I))):
        el.decompose()


def _text_with_italics(el: Tag) -> tuple[str, list[tuple[int, int]]]:
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0

    def walk(node: Tag, italic: bool) -> None:
        nonlocal pos
        for child in node.children:
            if isinstance(child, str):
                s = str(child).replace("\xa0", " ").replace("\r", "")
                if not s:
                    continue
                parts.append(s)
                if italic and s.strip():
                    spans.append((pos, pos + len(s)))
                pos += len(s)
            elif isinstance(child, Tag):
                if child.name == "br":
                    parts.append("\n")
                    pos += 1
                else:
                    walk(child, italic or child.name in _ITALIC_TAGS)

    walk(el, el.name in _ITALIC_TAGS)

    raw = "".join(parts)
    lead = len(raw) - len(raw.lstrip())
    trimmed = raw.strip()
    adjusted: list[tuple[int, int]] = []
    for a, b in _merge_spans(spans):
        a, b = a - lead, b - lead
        a, b = max(0, a), min(len(trimmed), b)
        if b > a:
            adjusted.append((a, b))
    return trimmed, adjusted


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans = sorted(spans)
    out = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _collapse_inline_ws(s: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", s.replace("\n", " ")).strip()


def _select_chapter_content(soup: BeautifulSoup) -> Tag | None:
    for sel in ("div.chapter-content", "div.chapter-inner", "div.chapter-page"):
        node = soup.select_one(sel)
        if node:
            return node
    divs = soup.find_all("div")
    return max(divs, key=lambda d: len(d.find_all("p")), default=None)


def _blocks_from_content(content: Tag) -> list[Block]:
    blocks: list[Block] = []
    children = [c for c in content.find_all(recursive=False) if isinstance(c, Tag)]
    if not children:  # some fictions dump loose text with no wrapping <p>
        children = [c for c in content.children if isinstance(c, Tag)]

    for el in children:
        name = (el.name or "").lower()
        if name == "hr":
            blocks.append(Block("scene_break"))
            continue
        if name in _HEADING_TAGS:
            text = _collapse_inline_ws(el.get_text(" ", strip=True))
            if text:
                blocks.append(Block("heading", text))
            continue

        raw, spans = _text_with_italics(el)
        if not raw.strip():
            continue

        flat = _collapse_inline_ws(raw)
        if _BREAK_GLYPHS_RE.match(flat):
            blocks.append(Block("scene_break"))
            continue

        chat = _parse_chat(flat)
        if chat:
            blocks.append(Block("chat", chat["message"],
                                meta={"user": chat["user"], "location": chat["location"]}))
            continue

        if name in _SYSTEM_TAGS or flat.startswith("[") and flat.rstrip().endswith("]") \
                or _SYSTEM_HINT_RE.search(flat):
            blocks.append(Block("system", flat.strip("[]").strip()))
            continue

        blocks.append(Block("paragraph", raw, italic=spans))
    return blocks


def _meta_from_head(soup: BeautifulSoup) -> dict:
    def prop(*names: str) -> str:
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find(
                "meta", attrs={"name": n}
            )
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    og_title = prop("og:title", "twitter:title")
    chapter_title, fiction_title = og_title, ""
    if " - " in og_title:
        chapter_title, fiction_title = (p.strip() for p in og_title.split(" - ", 1))
    if not chapter_title:
        h1 = soup.find("h1")
        chapter_title = h1.get_text(" ", strip=True) if h1 else ""

    next_url = ""
    for a in soup.find_all("a", href=True):
        if a.get_text(" ", strip=True).lower().startswith("next chapter"):
            next_url = a["href"]
            break

    return {
        "chapter_title": chapter_title,
        "fiction_title": fiction_title,
        "url": prop("og:url"),
        "next_url": next_url,
    }


def parse_document(html: str, *, url: str = "") -> Document:
    soup = BeautifulSoup(html, "lxml")
    content = _select_chapter_content(soup)
    if content is None:
        raise SystemExit("could not locate chapter content in the page")

    _strip_hidden(content, _hidden_class_names(soup))
    blocks = _blocks_from_content(content)

    head = _meta_from_head(soup)
    return Document(
        blocks=blocks,
        fiction_title=head["fiction_title"],
        chapter_title=head["chapter_title"],
        author=_author(soup),
        url=url or head["url"],
        next_url=head["next_url"],
    )


def _author(soup: BeautifulSoup) -> str:
    a = soup.select_one('.fic-header a[href*="/profile/"], a[href*="/profile/"]')
    return a.get_text(" ", strip=True) if a else ""
