"""Site-agnostic HTML -> Blocks toolkit for providers that read web pages.

Nothing here knows any site. A provider picks the content container, title
and author out of its own markup, then hands the container to
`blocks_from_container`. Two jobs matter here:

  1. Drop hidden text. Some sites inject decoy paragraphs hidden by a CSS
     rule (`.<random>{display:none}`); `hidden_class_names` collects every
     class a page's stylesheets hide and `strip_hidden` removes those nodes
     plus inline `display:none`, `hidden` and `aria-hidden`.

  2. Preserve italics. Whole-sentence / whole-paragraph italics in this genre
     are internal monologue; `segment.build_segments` routes those to the
     `thought` voice, so italic character ranges are recorded on each block.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from ..normalize import Block

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


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def parse_chat(text: str) -> dict | None:
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


def hidden_class_names(soup: BeautifulSoup) -> set[str]:
    hidden: set[str] = set()
    for style in soup.find_all("style"):
        css = style.string or style.get_text() or ""
        for match in _HIDE_RULE_RE.finditer(css):
            for selector in match.group(1).split(","):
                hidden.update(_CLASS_IN_SELECTOR_RE.findall(selector))
    return hidden


def strip_hidden(container: Tag, hidden_classes: set[str]) -> None:
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


def drop_matching(container: Tag, class_pattern: str) -> None:
    """Remove every element whose class matches `class_pattern` (a regex)."""
    for el in list(container.find_all(class_=re.compile(class_pattern, re.I))):
        el.decompose()


def text_with_italics(el: Tag) -> tuple[str, list[tuple[int, int]]]:
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


def collapse_inline_ws(s: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", s.replace("\n", " ")).strip()


def guess_content_container(soup: BeautifulSoup) -> Tag | None:
    """Last resort for markup nobody has taught us: the div with most <p>s.
    Beware it can pick a whole-page wrapper; providers should select their
    container explicitly and only fall back to this."""
    divs = soup.find_all("div")
    return max(divs, key=lambda d: len(d.find_all("p")), default=None)


def meta_content(soup: BeautifulSoup, *names: str) -> str:
    """First non-empty <meta property|name=…> content among `names`."""
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find(
            "meta", attrs={"name": n}
        )
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def blocks_from_container(content: Tag) -> list[Block]:
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
            text = collapse_inline_ws(el.get_text(" ", strip=True))
            if text:
                blocks.append(Block("heading", text))
            continue

        raw, spans = text_with_italics(el)
        if not raw.strip():
            continue

        flat = collapse_inline_ws(raw)
        if _BREAK_GLYPHS_RE.match(flat):
            blocks.append(Block("scene_break"))
            continue

        chat = parse_chat(flat)
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


def next_link_by_text(soup: BeautifulSoup, prefix: str = "next chapter") -> str:
    for a in soup.find_all("a", href=True):
        if a.get_text(" ", strip=True).lower().startswith(prefix):
            return a["href"]
    return ""
