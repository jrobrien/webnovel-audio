"""Flag known heteronyms for human review — never auto-corrected.

A heteronym's pronunciation depends on meaning/part of speech ("a *tear* in
her eye" vs. "a *tear* in the fabric"), which the g2p can't reliably resolve
from spelling alone. There's no safe blanket lexicon fix for one of these —
the right reading changes occurrence to occurrence — so this only surfaces
where they appear, with a bit of context, for you to judge by ear. Add or
remove words freely; it's a plain set.
"""
from __future__ import annotations

import re

# Common English heteronyms worth a second look in fiction narration. Not
# exhaustive — Latin-stress noun/verb pairs (CON-tent/con-TENT) are the bulk
# of it, plus a handful of everyday words with an unrelated second reading.
HETERONYMS: set[str] = {
    "tear", "bow", "wind", "lead", "read", "live", "wound", "bass", "close",
    "does", "minute", "object", "produce", "project", "content", "desert",
    "present", "record", "refuse", "use", "invalid", "moderate", "separate",
    "house", "excuse", "entrance", "contract", "conduct", "console", "convict",
    "defect", "digest", "discount", "escort", "export", "import", "insult",
    "permit", "rebel", "subject", "suspect", "address", "attribute", "combine",
    "compact", "compound", "compress", "conflict", "contest", "contrast",
    "decrease", "increase", "extract", "incline", "intern", "perfect",
    "progress", "protest", "recall", "reject", "survey", "polish", "sow",
    "dove", "row", "buffet", "resume", "attribute", "graduate", "advocate",
    "estimate", "elaborate", "delegate", "duplicate", "articulate",
}

_STRIP = "\"'’“”‘()[]{}.,!?;:…—–*"


def find_heteronyms(text: str, *, context: int = 6) -> list[tuple[str, int, str]]:
    """[(word, occurrence count, one example context snippet)], most frequent first.

    `context` is words of context on each side of the first occurrence shown.
    Matching is whole-word and case-insensitive against `HETERONYMS`.
    """
    words = text.split()
    hits: dict[str, list[int]] = {}
    for i, w in enumerate(words):
        core = w.strip(_STRIP).lower()
        if core in HETERONYMS:
            hits.setdefault(core, []).append(i)

    out = []
    for word, idxs in hits.items():
        i0 = idxs[0]
        lo, hi = max(0, i0 - context), min(len(words), i0 + context + 1)
        snippet = re.sub(r"\s+", " ", " ".join(words[lo:hi])).strip()
        out.append((word, len(idxs), snippet))
    return sorted(out, key=lambda t: (-t[1], t[0]))
