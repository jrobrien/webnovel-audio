"""The canonical in-memory chapter: what every provider produces and everything
downstream (normalize -> segment -> synth, Markdown, feed) consumes."""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field

from .normalize import Block


@dataclass
class Document:
    blocks: list[Block]
    fiction_title: str = ""
    chapter_title: str = ""
    author: str = ""
    url: str = ""
    next_url: str = ""
    meta: dict = field(default_factory=dict)
    # provenance, filled in by stamp_provenance
    raw_sha256: str = ""
    raw_bytes: int = 0
    retrieved_at: str = ""


def stamp_provenance(doc: Document, raw_text: str, *, source_path: str = "") -> Document:
    """Record what the Document was made from. Done by the caller of
    `Provider.parse`, never by the provider, so no provider can forget it."""
    data = raw_text.encode("utf-8", "replace")
    doc.raw_sha256 = hashlib.sha256(data).hexdigest()
    doc.raw_bytes = len(data)
    if source_path and os.path.exists(source_path):
        doc.retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%S",
                                         time.localtime(os.path.getmtime(source_path)))
    else:
        doc.retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    return doc
