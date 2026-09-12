"""Rule-based dialogue splitting + speaker attribution.

No heavy NLP dependency: a quote scanner plus tag / pronoun / subject / sticky
heuristics. It gets the common web-novel cases right (explicit `"...," said X`,
`X said, "..."`, `he muttered`, back-and-forth volleys, untagged continuations)
and everything it decides is shown by `inspect` / `cast` and overridable in
`[cast.voices]`. A BookNLP-backed attributor could implement the same
`Attributor.attribute` surface later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .normalize import Block, dequote_preserving
from .segment import split_with_offsets

_SPEECH_VERBS = {
    "said", "says", "asked", "asks", "replied", "replies", "answered", "answers",
    "shouted", "yelled", "called", "whispered", "muttered", "mumbled", "murmured",
    "growled", "snarled", "hissed", "snapped", "barked", "grumbled", "sighed",
    "laughed", "chuckled", "giggled", "cried", "exclaimed", "added", "continued",
    "began", "offered", "admitted", "insisted", "warned", "teased", "joked",
    "countered", "interrupted", "breathed", "stated", "noted", "observed",
    "remarked", "spat", "drawled", "quipped", "retorted", "responded", "repeated",
    "announced", "declared", "protested", "pleaded", "demanded", "ordered",
    "echoed", "finished", "spoke", "gasped", "wheezed", "rasped",
}
_PERSON_NOUNS = {
    "man", "woman", "boy", "girl", "knight", "crone", "seer", "spirit", "ghost",
    "elf", "guard", "soldier", "king", "queen", "lord", "lady", "stranger",
    "figure", "voice", "priest", "priestess", "witch", "wizard", "mage", "child",
    "warrior", "captain", "commander", "monk", "nun", "maiden", "elder", "dwarf",
    "giant", "goblin", "demon", "girl", "servant", "master", "boy",
}
_MALE_P = {"he", "him", "his", "himself"}
_FEMALE_P = {"she", "her", "hers", "herself"}
_PREPS = {
    "at", "to", "with", "for", "of", "from", "near", "beside", "behind", "on",
    "against", "toward", "towards", "upon", "about", "over", "under", "by",
}
_NOT_A_NAME = {
    "The", "A", "An", "He", "She", "They", "It", "His", "Her", "Their", "I",
    "We", "You", "That", "This", "There", "Then", "But", "And", "So", "As",
    "With", "When", "While", "If", "Yeah", "Ahh", "Oh", "Well", "No", "Gods",
    "God", "Not", "Now", "Only", "Just", "Man", "Alright", "Really", "Last",
    "From", "Every", "Right", "Sorry", "Lord", "Lady", "Sir", "Mister",
    "Obviously", "Suddenly", "Finally", "Perhaps", "Maybe", "Somehow", "Instead",
    "Meanwhile", "Anyway", "Besides", "However", "Later", "Soon", "Once", "After",
    "Before", "Since", "Because", "Though", "Although", "What", "One", "Two",
    "Everyone", "Someone", "Nobody", "Something", "Nothing",
}
_NAME_RE = re.compile(r"\b([A-Z][a-z]{2,})\b")
_WORD_RE = re.compile(r"[A-Za-z]+")
_LOWER_RE = re.compile(r"[a-z]{3,}")
_VOCATIVE_RE = re.compile(r",\s*([A-Z][a-z]{2,})[.!?…\"']*\s*$")


@dataclass
class Line:
    text: str
    kind: str          # narration | dialogue | thought
    speaker: str = ""
    is_para_end: bool = False


def _split_quotes(s: str) -> list[tuple[str, bool, int, int]]:
    """Ordered (text, is_quote, start, end) pieces. Handles an unclosed final quote."""
    out: list[tuple[str, bool, int, int]] = []
    i = 0
    n = len(s)
    while i < n:
        q = s.find('"', i)
        if q == -1:
            if s[i:].strip():
                out.append((s[i:], False, i, n))
            break
        if q > i and s[i:q].strip():
            out.append((s[i:q], False, i, q))
        close = s.find('"', q + 1)
        if close == -1:
            out.append((s[q + 1:], True, q + 1, n))
            break
        out.append((s[q + 1:close], True, q + 1, close))
        i = close + 1
    return out


def _tag_from(text: str) -> tuple[str, str] | None:
    """Return ('name', X) or ('pron', 'he'/'she'/'they') if `text` looks like a
    speech tag adjacent to a quote."""
    words = _WORD_RE.findall(text)
    low = [w.lower() for w in words]
    for idx, w in enumerate(low):
        if w in _SPEECH_VERBS:
            before = words[idx - 1] if idx else ""
            after = words[idx + 1] if idx + 1 < len(words) else ""
            for cand in (before, after):
                if cand.lower() in _MALE_P:
                    return ("pron", "he")
                if cand.lower() in _FEMALE_P:
                    return ("pron", "she")
                if cand[:1].isupper() and cand not in _NOT_A_NAME and len(cand) > 2:
                    return ("name", cand)
    return None


class Attributor:
    def __init__(self, cfg):
        self.protagonist = (cfg.cast.protagonist or "").strip()
        self.known: set[str] = set(cfg.cast.voices)
        if self.protagonist:
            self.known.add(self.protagonist)
        self.gender: dict[str, str] = {}
        self.last_male = ""
        self.last_female = ""
        self.sticky = ""            # most recent dialogue speaker
        self.narr_run = 0           # consecutive narration-only blocks
        self.prev_tail = ""         # trailing narration of the previous block

    # -- narration bookkeeping -------------------------------------------------
    def observe(self, text: str) -> None:
        # Only mid-sentence capitalised words count as names, so sentence openers
        # ("Obviously", "Suddenly", "Cardio was not his forte.") can't be taken
        # for characters.
        for m in _NAME_RE.finditer(text):
            name = m.group(1)
            if name in _NOT_A_NAME:
                continue
            j = m.start() - 1
            while j >= 0 and text[j] in " \t\n":
                j -= 1
            if j >= 0 and text[j] in ".!?…":   # sentence-opener adverb, not a name
                continue
            words = _WORD_RE.findall(text[m.end(): m.end() + 60])[:8]
            for k, w in enumerate(words):
                lw = w.lower()
                if lw not in _MALE_P and lw not in _FEMALE_P:
                    continue
                if k and words[k - 1].lower() in _PREPS:
                    continue  # "looked up at her", "pointed to him" -> object, not a cue
                if lw in _MALE_P:
                    self.gender[name], self.last_male = "m", name
                else:
                    self.gender[name], self.last_female = "f", name
                break

    # -- attribution --------------------------------------------------------
    def _resolve_pron(self, pron: str) -> str:
        want = {"he": "m", "she": "f"}.get(pron, "")
        if not want:
            return self.sticky
        if self.sticky and self.gender.get(self.sticky) == want:
            return self.sticky
        return (self.last_male if want == "m" else self.last_female) or self._recent_of(want)

    def _recent_of(self, g: str) -> str:
        for name, gg in self.gender.items():
            if gg == g:
                return name
        return ""

    def _subject(self, sentence: str) -> str:
        m = _NAME_RE.match(sentence.lstrip())
        if m:
            cand = m.group(1)
            if cand not in _NOT_A_NAME and (cand in self.known or cand in self.gender):
                return cand
        # a descriptive referent ("the wrinkled crone ...") -> its head noun
        for w in _LOWER_RE.findall(sentence.lower())[:5]:
            if w in _PERSON_NOUNS:
                return w
        return ""

    def attribute(self, quote: str, pre: str, post: str) -> str:
        speaker = ""
        for ctx in (post, pre):
            tag = _tag_from(ctx) if ctx else None
            if not tag:
                continue
            kind, val = tag
            speaker = val if kind == "name" else self._resolve_pron(val)
            if speaker:
                break

        if not speaker and pre:
            last_sentence = re.split(r"(?<=[.!?…])\s+", pre.strip())[-1]
            speaker = self._subject(last_sentence)

        if not speaker and not pre and self.prev_tail:
            last_sentence = re.split(r"(?<=[.!?…])\s+", self.prev_tail.strip())[-1]
            speaker = self._subject(last_sentence)

        if not speaker:
            if self.narr_run < 2 and self.sticky:
                speaker = self.sticky
            elif self.protagonist:
                speaker = self.protagonist

        addressed = _VOCATIVE_RE.search(quote)
        if addressed and addressed.group(1) == speaker and self.sticky and self.sticky != speaker:
            speaker = self.sticky  # you don't narrate your own name as a vocative

        if speaker:
            self.sticky = speaker
        return speaker


def split_paragraph(block: Block, attr: Attributor, cfg) -> list[Line]:
    pre = dequote_preserving(block.text)
    pieces = _split_quotes(pre)
    lines: list[Line] = []
    had_quote = False

    for i, (text, is_quote, start, end) in enumerate(pieces):
        if is_quote:
            had_quote = True
            ctx_pre = pieces[i - 1][0] if i and not pieces[i - 1][1] else ""
            ctx_post = pieces[i + 1][0] if i + 1 < len(pieces) and not pieces[i + 1][1] else ""
            speaker = attr.attribute(text, ctx_pre, ctx_post)
            for sent, _a, _b in split_with_offsets(text):
                lines.append(Line(sent, "dialogue", speaker))
        else:
            attr.observe(text)
            for sent, a, b in split_with_offsets(text):
                ratio = _italic_ratio(start + a, start + b, block.italic)
                lines.append(Line(sent, "thought" if ratio >= cfg.synth.thought_threshold
                                  else "narration"))

    if pieces and not pieces[-1][1]:
        attr.prev_tail = pieces[-1][0]
    attr.narr_run = 0 if had_quote else attr.narr_run + 1
    if lines:
        lines[-1].is_para_end = True
    return lines


MALE_VOICE_POOL = [
    "am_michael", "bm_lewis", "am_puck", "bm_george", "am_eric", "am_onyx",
    "bm_daniel", "am_fenrir", "am_liam", "bm_fable",
]
FEMALE_VOICE_POOL = [
    "af_heart", "bf_emma", "af_bella", "af_nicole", "bf_alice", "af_sarah",
    "af_sky", "bf_isabella", "af_aoede", "af_kore",
]


def suggest_voices(counts: dict, gender: dict, cfg) -> dict:
    """{speaker: Kokoro voice id} — round-robins the sex-matched pool by frequency,
    skipping voices already spoken for in `[cast.voices]`. Used by `cast` and by
    `check --write`'s cast scaffolding (see `sync.suggest_cast`)."""
    used = set(cfg.cast.voices.values())
    m = (v for v in MALE_VOICE_POOL if v not in used)
    f = (v for v in FEMALE_VOICE_POOL if v not in used)
    out: dict[str, str] = {}
    for name in sorted(counts, key=lambda n: -counts[n]):
        if not name:
            continue
        if name in cfg.cast.voices:
            out[name] = cfg.cast.voices[name]
            continue
        pool = f if gender.get(name) == "f" else m
        out[name] = next(pool, cfg.cast.default or cfg.voices.dialogue_default)
    return out


def discover(blocks: list[Block], cfg) -> tuple[dict[str, int], dict[str, str]]:
    """Run attribution over a chapter; return {speaker: line count} and a gender guess."""
    from collections import Counter

    attr = Attributor(cfg)
    counts: Counter[str] = Counter()
    for block in blocks:
        if block.kind != "paragraph":
            if block.kind in ("heading", "system"):
                attr.narr_run += 1
            continue
        for line in split_paragraph(block, attr, cfg):
            if line.kind == "dialogue":
                counts[line.speaker or ""] += 1
    return dict(counts), dict(attr.gender)


def _italic_ratio(start: int, end: int, spans: list[tuple[int, int]]) -> float:
    total = end - start
    if total <= 0:
        return 0.0
    covered = 0
    for a, b in spans:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            covered += hi - lo
    return covered / total
