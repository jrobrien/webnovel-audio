"""Blocks -> a flat segment script (the unit of synthesis + caching).

Phase 2: sentences inside an italic run become `thought` segments (routed to the
`thought` voice); scene breaks become pauses; headings and LitRPG system boxes
get their own styles/voices. Per-sentence italic coverage is measured on the raw
block text (offsets preserved) before spoken-form normalization rewrites it.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass

from .normalize import (
    Block,
    normalize_chat_message,
    normalize_heading,
    normalize_system,
    normalize_text,
    normalize_username,
)

_ABBREV = r"(?:Mr|Mrs|Ms|Dr|Lt|Sgt|Sr|Jr|St|vs|etc|Gen|Capt|Col|Cmdr|Adm|Prof)"

_TERMINATORS = ".!?…"
_TRAILERS = ".!?…\"')]"
_ABBREV_GUARD_RE = re.compile(rf"\b{_ABBREV}\.")
_ABBREV_MARK = "\uf8ff"  # equal-length placeholder for a masked abbreviation period

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+")
_STOPWORDS = set(
    """A About After Ahh Alright An And As At Because Before But By Can Could Did Do Down
    Even Every For From Gods God Had Has Have He Her Here His How I If In Is It Its Just
    Last Let Like Man Maybe My No Nor Not Now Of Oh On Once Only Or Really Right She Since
    So Some Still That The Their Then There These They This Those To Up Was We Well What
    When Where While Who Why With Yeah Yes You Your Lord Lady Church Sir""".split()
)


@dataclass
class Segment:
    text: str
    voice: str
    style: str = "narration"      # narration | thought | dialogue | system | heading | chat
    rate: float = 1.0
    pitch: float = 0.0
    pause_after_ms: int = 0
    kind: str = "speech"          # speech | pause | cue
    speaker: str = ""             # attributed character / chat handle, for inspection

    def cache_key_material(self) -> str:
        return f"{self.text}|{self.voice}|{self.style}|{self.rate}|{self.pitch}"


def split_sentences(text: str) -> list[str]:
    """Offset-free splitter (used for headings, system boxes, tests)."""
    return [s for s, _, _ in split_with_offsets(text)]


def split_with_offsets(s: str) -> list[tuple[str, int, int]]:
    """Split into sentences, returning (sentence, start, end) on the *input* string.

    Abbreviation periods are masked with an equal-length placeholder so offsets
    stay aligned with any italic spans computed on the same string.
    """
    protected = _ABBREV_GUARD_RE.sub(lambda m: m.group(0)[:-1] + _ABBREV_MARK, s)
    out: list[tuple[str, int, int]] = []
    n = len(protected)
    start = 0
    i = 0
    while i < n:
        if protected[i] in _TERMINATORS:
            j = i + 1
            while j < n and protected[j] in _TRAILERS:
                j += 1
            k = j
            while k < n and protected[k] in " \t\n":
                k += 1
            at_boundary = k >= n or protected[k].isupper() or protected[k] in "\"'(["
            if at_boundary:
                _emit(out, s, start, j)
                start = k
                i = k
                continue
        i += 1
    _emit(out, s, start, n)
    return out


def _emit(out: list[tuple[str, int, int]], src: str, start: int, end: int) -> None:
    raw = src[start:end]
    stripped = raw.strip()
    if not stripped:
        return
    lead = len(raw) - len(raw.lstrip())
    a = start + lead
    out.append((stripped.replace(_ABBREV_MARK, "."), a, a + len(stripped)))


def _finish(text: str, lexicon, nlp=None) -> str:
    """Rewrite one sentence into what the TTS should actually say.

    One table, one pass: `Lexicon.apply` resolves phrases, part-of-speech rules
    and plain rules together, most specific first, so there is no ordering
    question between "the lexicon" and "the tagger" any more — they are the
    same thing.
    """
    return lexicon.apply(text, nlp) if lexicon is not None else text


def _voice_for(line, cfg) -> tuple[str, str]:
    """(voice_id, style) for a narration/thought/dialogue Line."""
    if line.kind == "thought":
        return cfg.voices.thought, "thought"
    if line.kind == "dialogue":
        cast = cfg.cast.voices
        # An empty value is a deliberate "listed but unassigned" — `cast update`
        # writes those for speakers whose gender it couldn't guess, so they're
        # visible to edit rather than silently given a coin-flip voice.
        if line.speaker and cast.get(line.speaker):
            return cast[line.speaker], "dialogue"
        return (cfg.cast.default or cfg.voices.dialogue_default), "dialogue"
    return (cfg.cast.narrator or cfg.voices.narrator), "narration"


def _assign_chat_voice(user: str, cfg, assigned: dict[str, str]) -> str:
    """Deterministic per-handle voice: hash into the pool, but the first distinct
    handles get distinct voices (so a two-way exchange never shares a voice)."""
    if user in assigned:
        return assigned[user]
    pinned = cfg.chat.voices_by_user.get(user)
    if pinned:
        assigned[user] = pinned
        return pinned
    pool = cfg.chat.voices or [cfg.voices.dialogue_default]
    pick = pool[int(hashlib.sha1(user.encode()).hexdigest(), 16) % len(pool)]
    used = set(assigned.values())
    if pick in used and len(used) < len(pool):
        pick = next(v for v in pool if v not in used)
    assigned[user] = pick
    return pick


def build_segments(blocks: list[Block], cfg, lexicon=None, nlp=None) -> list[Segment]:
    from .dialogue import Attributor, split_paragraph

    attr = Attributor(cfg)
    segs: list[Segment] = []
    seen_chat: set[str] = set()
    chat_voices: dict[str, str] = {}
    chat_run_open = False

    for block in blocks:
        if block.kind == "chat":
            user = block.meta.get("user", "")
            location = block.meta.get("location", "")
            voice = _assign_chat_voice(user, cfg, chat_voices)
            crate = round(cfg.synth.speed * cfg.chat.rate, 3)

            if not chat_run_open and cfg.chat.earcon:
                segs.append(Segment("", "", style="chat", kind="cue", pause_after_ms=90))
            chat_run_open = True

            mode = cfg.chat.speak_username
            if mode == "always" or (mode == "first" and user not in seen_chat):
                lead = normalize_username(user)
                if cfg.chat.speak_location and location:
                    lead = f"{lead}, from {location}"
                if lead:
                    segs.append(Segment(text=_finish(lead, lexicon, nlp), voice=voice,
                                        style="chat", speaker=user, rate=crate,
                                        pause_after_ms=160))
            seen_chat.add(user)

            msg = normalize_chat_message(block.text, dampen_caps=cfg.chat.dampen_caps)
            sentences = split_sentences(msg) or ([msg] if msg else [])
            for i, sent in enumerate(sentences):
                text = _finish(sent, lexicon, nlp)
                if not text:
                    continue
                last = i == len(sentences) - 1
                segs.append(Segment(
                    text=text, voice=voice, style="chat", speaker=user, rate=crate,
                    pause_after_ms=cfg.pauses.chat_ms if last else cfg.pauses.sentence_ms,
                ))
            continue

        chat_run_open = False

        if block.kind == "scene_break":
            segs.append(Segment("", "", kind="pause",
                                pause_after_ms=cfg.pauses.scene_break_ms))
            continue

        if block.kind == "heading":
            text = _finish(normalize_heading(block.text), lexicon, nlp)
            fic = _finish(normalize_text(block.meta.get("fiction", "")), lexicon, nlp).strip(" .")
            if fic and text:
                text = f"{fic}. {text}"          # "Salvage Run. Chapter One. Dead Air."
            if text and text[-1] not in ".!?…":
                text += "."                     # unterminated -> Kokoro clips the last word
            if text:
                segs.append(Segment(text=text,
                                    voice=cfg.cast.narrator or cfg.voices.narrator,
                                    style="heading", rate=cfg.synth.speed,
                                    pause_after_ms=cfg.pauses.scene_break_ms))
            continue

        if block.kind == "system":
            sentences = split_sentences(normalize_system(block.text)) or [
                normalize_system(block.text)
            ]
            for sent in sentences:
                text = _finish(sent, lexicon, nlp)
                if not text:
                    continue
                segs.append(Segment(
                    text=text, voice=cfg.voices.system_ui, style="system",
                    rate=round(cfg.synth.speed * cfg.synth.system_rate, 3),
                    pause_after_ms=cfg.pauses.sentence_ms,
                ))
            if segs and segs[-1].style == "system":
                segs[-1].pause_after_ms = cfg.pauses.paragraph_ms
            continue

        # paragraph: quote splitting + speaker attribution + italic -> thought
        for line in split_paragraph(block, attr, cfg):
            text = _finish(normalize_text(line.text), lexicon, nlp)
            if not text:
                continue
            voice, style = _voice_for(line, cfg)
            pause = cfg.pauses.paragraph_ms if line.is_para_end else cfg.pauses.sentence_ms
            if style == "dialogue":
                pause = max(pause, cfg.pauses.dialogue_ms)
            if text.endswith(("…", "...")):
                pause += cfg.pauses.ellipsis_extra_ms
            segs.append(Segment(
                text=text, voice=voice, style=style, speaker=line.speaker,
                rate=cfg.synth.speed, pause_after_ms=pause,
            ))
    return segs


def segments_to_json(segments: list[Segment]) -> str:
    return json.dumps([asdict(s) for s in segments], ensure_ascii=False, indent=2)


def find_unknown_names(blocks: list[Block], known: set[str]) -> list[tuple[str, int]]:
    """Capitalized words that recur mid-sentence and aren't in the lexicon.

    Heuristic review queue for building the per-series pronunciation dict.
    """
    counts: Counter[str] = Counter()
    for block in blocks:
        if block.kind not in ("paragraph", "system"):
            continue
        tokens = block.text.split()
        for j, token in enumerate(tokens):
            m = _WORD_RE.match(token)
            if not m:
                continue
            word = m.group(0)
            if len(word) < 3 or not word[0].isupper() or not word[1:].isalpha():
                continue
            if not word[1:].islower():
                continue
            if word in _STOPWORDS or word in known:
                continue
            prev = tokens[j - 1].rstrip("\"'") if j else ""
            sentence_initial = j == 0 or prev.endswith((".", "!", "?", "…", '"'))
            if sentence_initial:
                continue  # ambiguous: could just be a sentence-start common word
            counts[word] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
