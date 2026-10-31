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

import difflib
import re

from bs4 import BeautifulSoup, Tag

from ..normalize import Block, int_to_words

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
    "designation", "species", "race", "class", "rank", "level", "skill", "title",
}
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. ]*$")
_PAREN_OR_NON_ASCII_RE = re.compile(r"\([^)]*\)|[^\x00-\x7f]")
_STAT_VALUE_RE = re.compile(r"[\d\s.,/|%+\-:=<>]*")
_HEADER_RE = re.compile(r"^<<\s*([^<>]+?)\s*>>$")


def _is_stat_value(message: str) -> bool:
    """`15 ->`, `577/577`, `6|30`, `0/27 (Light)`, `2,384 क`: numbers and symbols
    only, once parenthesised notes and non-ASCII glyphs are set aside."""
    core = _PAREN_OR_NON_ASCII_RE.sub(" ", message)
    return any(ch.isdigit() for ch in core) and bool(_STAT_VALUE_RE.fullmatch(core))


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
    if not location and user.isalpha() and user.isupper() and len(user) >= 3:
        return None  # [NAME: …], [TYPE: …]: a capitalised label, not a viewer
    if not location and user.isalpha() and _is_stat_value(message):
        return None  # [HP: 577/577], [Ambition: 15 ->]: a stat line, not a viewer
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


def system_clause_text(raw: str) -> str:
    """A boxed stat readout's `<br>` line breaks, as spoken clause breaks.

    Authors lay these out one stat per line (`Level: 7<br>Archetype:
    Watcher<br>...`); `collapse_inline_ws` flattens that straight to spaces
    like ordinary prose, which runs every "label: value" into the next with
    nothing between them. Turning each line break into a full stop instead
    gives each stat its own TTS sentence/pause, while the colon inside a
    line still binds "label: value" together. A bare header line with no
    value of its own (`Attributes:`) stays glued to what follows it.
    """
    lines = [collapse_inline_ws(ln) for ln in raw.replace("\r", "").split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    out = lines[0]
    for ln in lines[1:]:
        out += (" " if out.endswith(":") else ". ") + ln
    return out


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

        if name == "div" and el.find("table") is not None:
            el, name = el.find("table"), "table"        # a box wrapped in a scroll div
        if name == "table":
            blocks.extend(_table_blocks(el))
            continue

        raw, spans = text_with_italics(el)
        if not raw.strip():
            continue

        flat = collapse_inline_ws(raw)
        if _BREAK_GLYPHS_RE.match(flat):
            blocks.append(Block("scene_break"))
            continue

        header = _HEADER_RE.match(flat)
        if header:      # `<<ATTRIBUTES>>`: a stat-box section title
            title = header.group(1)
            blocks.append(Block("system", (title.title() if title.isupper() else title) + "."))
            continue

        bracketed = flat.startswith("[") and flat.rstrip().endswith("]")
        chat = parse_chat(flat)
        if chat:
            blocks.append(Block("chat", chat["message"],
                                meta={"user": chat["user"], "location": chat["location"],
                                      "bracket": raw}))
            continue

        if name in _SYSTEM_TAGS or bracketed or _SYSTEM_HINT_RE.search(flat):
            blocks.append(Block("system", system_clause_text(raw).strip("[]").strip(),
                                meta={"bracket": raw} if bracketed else {}))
            continue

        blocks.append(Block("paragraph", raw, italic=spans))
    return _stat_runs_are_system(blocks)


def _table_blocks(table: Tag) -> list[Block]:
    """A table is a boxed system readout: one system block per line in it.

    Authors build the stat box as a one-cell table of `<p>` lines, or as a row per
    line (`Name | Mara`). Flattened into one paragraph it ran together and was read
    in the narrator's voice, so a cell's `<p>` lines are each a block and a row of
    plain cells is one."""
    out: list[Block] = []

    def add(parts: list[str]) -> None:
        text = system_clause_text(" ".join(p for p in parts if p.strip())).strip("[]").strip()
        if any(ch.isalnum() for ch in text):
            out.append(Block("system", text))

    for row in table.find_all("tr") or [table]:
        parts: list[str] = []
        for cell in row.find_all(["td", "th"]) or [row]:
            lines = cell.find_all("p")
            if len(lines) > 1:
                add(parts)
                parts = []
                for line in lines:
                    add([text_with_italics(line)[0]])
            else:
                parts.append(text_with_italics(cell)[0])
        add(parts)
    return out


def _stat_runs_are_system(blocks: list[Block]) -> list[Block]:
    """`[Host: Lu Qi]` is a stat line, not a viewer called Host.

    One bracketed line is ambiguous: `[Handle: message]` is a livestream chat line
    and `[Label: value]` is a system readout, and `parse_chat` can only tell them
    apart when the value is numeric. Neighbours settle it. A readout is a *run* of
    bracketed lines (one paragraph each, so not a block in the source), most of them
    unmistakably system (several-word labels, no colon, `Quest:`, `...`); a chat
    stream is a run of handles. Within a run where system lines outnumber the
    ambiguous ones (and there are at least two), the ambiguous ones are system too.
    Lines with a location (`[Handle (City): …]`) are always chat.
    """
    out = list(blocks)
    i = 0
    while i < len(out):
        if not out[i].meta.get("bracket"):
            i += 1
            continue
        j = i
        while j < len(out) and out[j].meta.get("bracket"):
            j += 1
        run = range(i, j)
        sysn = sum(out[k].kind == "system" for k in run)
        amb = [k for k in run if out[k].kind == "chat" and not out[k].meta.get("location")]
        if sysn >= 2 and sysn >= len(amb):
            for k in amb:
                src = out[k].meta["bracket"]
                out[k] = Block("system", system_clause_text(src).strip("[]").strip(),
                               meta={"bracket": src})
        i = j
    return out


_TITLE_NUM_RE = re.compile(r"^\s*(?:chapter|ch\.?)?\s*(\d{1,4})\s*[-\u2013\u2014:.)]*\s*(.*)$", re.I)
_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _squash(text: str) -> str:
    return _ALNUM_RE.sub(" ", text.lower()).strip()


def drop_title_echo(blocks: list[Block], chapter_title: str) -> list[Block]:
    """Remove the chapter title the author repeats at the top of the text.

    The heading is announced from the page's own metadata, so a body that opens
    with `Chapter 17` and then the title (often italic) says it all a second time,
    in the thought voice. Only the first few short blocks are considered, each must
    look like part of the title (the number, `chapter N`, the name, or any of those
    together; the name may differ by a typo, as it did in `Manfiest`), and the first
    block that is not stops the scan, so story text is never touched.
    """
    m = _TITLE_NUM_RE.match(chapter_title or "")
    num, rest = (m.group(1), m.group(2).strip()) if m else ("", (chapter_title or "").strip())
    nums = [n for n in (num.lstrip("0") or ("0" if num else ""),
                        _squash(int_to_words(int(num))) if num else "") if n]
    rests = [_squash(rest)] if _squash(rest) else []
    forms = set(rests)
    for n in nums:
        forms.add(n)
        forms.add(f"chapter {n}")
        for r in rests:
            forms.add(f"{n} {r}")
            forms.add(f"chapter {n} {r}")
    forms.discard("")
    drop = 0
    for b in blocks[:4]:
        text = _squash(b.text)
        if b.kind not in ("paragraph", "heading") or not text or len(b.text) > 120:
            break
        if not any(text == f or (len(f) > 6 and difflib.SequenceMatcher(None, text, f).ratio() >= 0.85)
                   for f in forms):
            break
        drop += 1
    return blocks[drop:]


def next_link_by_text(soup: BeautifulSoup, prefix: str = "next chapter") -> str:
    for a in soup.find_all("a", href=True):
        if a.get_text(" ", strip=True).lower().startswith(prefix):
            return a["href"]
    return ""
