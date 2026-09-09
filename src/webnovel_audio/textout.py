"""Render the parsed chapter (ingest Blocks) as a readable Markdown document.

This is a first-class output alongside the .opus: the distilled story text, decoy
paragraphs removed and structure recovered, *before* any spoken-form rewriting
(numbers, respellings, sentence splitting). It carries YAML front-matter for
provenance and feeds straight into `pandoc … -o book.epub`.
"""
from __future__ import annotations

import re

from . import __version__
from .ingest import Document
from .normalize import Block

_LEADING_MD = re.compile(r"^([#>*+\-=]|\d+\.)\s")


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _yaml(value: str) -> str:
    s = _CTRL_RE.sub(" ", str(value)).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s.strip()}"'


def _emphasize(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    out: list[str] = []
    prev = 0
    for a, b in sorted(spans):
        a = max(a, prev)
        if b <= a:
            continue
        seg = text[a:b]
        core = seg.strip()
        out.append(text[prev:a])
        if core and any(ch.isalnum() for ch in core):
            lead = seg[: len(seg) - len(seg.lstrip())]
            trail = seg[len(seg.rstrip()):]
            out.append(f"{lead}*{core}*{trail}")   # keep whitespace outside the marks
        else:
            out.append(seg)                        # a lone italic "quote"/paren -> not emphasis
        prev = b
    out.append(text[prev:])
    return "".join(out)


def _para(block: Block) -> str:
    text = _emphasize(block.text, block.italic)
    text = re.sub(r"\s*\n\s*", " ", text).strip()
    return _LEADING_MD.sub(r"\\\g<0>", text)


def _chat_line(block: Block) -> str:
    user = block.meta.get("user", "").strip()
    loc = block.meta.get("location", "").strip()
    head = f"{user} ({loc})" if loc else user
    return f"[{head}: {block.text.strip()}]"


def _front_matter(doc: Document, extra: dict | None) -> str:
    fields: list[tuple[str, str]] = [
        ("title", doc.chapter_title or "Untitled"),
        ("fiction", doc.fiction_title),
        ("author", doc.author),
    ]
    for key, val in (extra or {}).items():
        fields.append((key, val))
    cid = re.search(r"/chapter/(\d+)", doc.url or "")
    fields += [
        ("source", doc.url),
        ("royalroad_id", cid.group(1) if cid else ""),
        ("retrieved", doc.retrieved_at),
        ("raw_sha256", doc.raw_sha256),
        ("raw_bytes", doc.raw_bytes),
        ("generator", f"webnovel-audio {__version__}"),
    ]
    lines = ["---"]
    for key, val in fields:
        if val in ("", None):
            continue
        lines.append(f"{key}: {val}" if isinstance(val, int) else f"{key}: {_yaml(val)}")
    lines.append("---")
    return "\n".join(lines)


def render_markdown(doc: Document, *, front_matter_extra: dict | None = None) -> str:
    title = doc.chapter_title or "Untitled"
    parts: list[str] = [_front_matter(doc, front_matter_extra), f"# {title}"]

    for block in doc.blocks:
        if block.kind == "scene_break":
            parts.append("* * *")
        elif block.kind == "heading":
            if block.text.strip() and block.text.strip() != title.strip():
                parts.append(f"## {block.text.strip()}")
        elif block.kind == "system":
            body = re.sub(r"\s*\n\s*", " ", block.text).strip()
            parts.append("\n".join(f"> {ln}" for ln in body.splitlines() or [body]))
        elif block.kind == "chat":
            parts.append(_chat_line(block))
        else:
            para = _para(block)
            if para:
                parts.append(para)

    return "\n\n".join(parts) + "\n"
