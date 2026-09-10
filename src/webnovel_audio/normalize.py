"""Text -> structured blocks + LitRPG-aware normalization.

Plain .txt splits on blank lines; HTML ingest (see `ingest.py`) produces the same
Block list but additionally carries `italic` character ranges so the segmenter can
route internal monologue to the `thought` voice.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

BlockKind = Literal["paragraph", "scene_break", "heading", "system", "chat"]

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)

_CURLY = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "′": "'", "″": '"', "«": '"', "»": '"',
}

_SCENE_BREAK_LITERAL = {"***", "* * *", "---", "###", "• • •", "· · ·", "= = ="}
_SCENE_BREAK_RE = re.compile(r"^\s*(?:[*#~•·\-—–_=]\s*){3,}\s*$")

_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

_ORDINAL_SMALL = {
    "1": "first", "2": "second", "3": "third", "4": "fourth", "5": "fifth",
    "6": "sixth", "7": "seventh", "8": "eighth", "9": "ninth", "10": "tenth",
    "11": "eleventh", "12": "twelfth", "13": "thirteenth",
}

_INT_RE = re.compile(r"(?<![\w$.])(\d{1,4})(?![\w.])")
_ORD_RE = re.compile(r"(?<![\w])(\d{1,4})(st|nd|rd|th)\b", re.IGNORECASE)
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?:\D|$))")
_LVL_RE = re.compile(r"\blvl\b\.?", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t]+")
_SPACE_PUNCT_RE = re.compile(r" +([,.;:!?])")
_SPACED_ELLIPSIS_RE = re.compile(r"\.\s\.\s\.|\. \. \.")
_DECIMAL_RE = re.compile(r"(?<![\w.])(\d{1,4})\.(\d{1,4})(?![\w.])")
_HASHNUM_RE = re.compile(r"#\s?(\d{1,4})\b")
_PLUSMINUS_RE = re.compile(r"(?<=\d)\s*([+\-])\s*(?=\d)")

# Prose-level abbreviation / symbol expansions (applied before number-to-words).
_TEXT_SUBS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\be\.g\.", re.I), "for example"),
    (re.compile(r"\bi\.e\.", re.I), "that is"),
    (re.compile(r"\betc\.(?=\s|$)", re.I), "et cetera"),
    (re.compile(r"\bvs\.?(?=\s|$)", re.I), "versus"),
    (re.compile(r"\bw/o\b", re.I), "without"),
    (re.compile(r"\bw/\b", re.I), "with"),
    (re.compile(r"\ba\.m\.", re.I), " AM"),
    (re.compile(r"\bp\.m\.", re.I), " PM"),
    (re.compile(r"\s*&\s*"), " and "),
    (re.compile(r"(?<=\d)\s*%"), " percent"),
    (re.compile(r"(?<=\d)\s*°"), " degrees"),
]
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿"
    "⌀-⏿⬀-⯿️‍]+"
)
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")
_LETNUM_RE = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
_ALLCAPS_RE = re.compile(r"\b([A-Z][A-Z'’]{3,})\b")

_CAPS_WORD_RE = re.compile(r"[A-Z][A-Z'’\-]*[A-Z]")   # 2+ all-caps letters
_ROMAN_RE = re.compile(r"[IVXLCDM]+\Z")
_CAPS_STRIP = "\"'’“”‘()[].,!?;:…—–*"


def _dampen_caps(s: str) -> str:
    """Fold shouted ALL-CAPS to normal case so the g2p doesn't spell it out.

    ("DAMN IT!" -> "damn it!"; espeak reads a short all-caps token as letters:
    IT -> "eye-tee", US -> "you-ess".)  A lone all-caps word is left alone unless
    it is <=2 letters or sits next to another all-caps word (a shouting run), so
    real acronyms ("the FBI", "a USB port", "Chapter IV") survive.
    """
    parts = re.split(r"(\s+)", s)
    words = parts[::2]
    core = [w.strip(_CAPS_STRIP) for w in words]
    isc = [bool(c) and _CAPS_WORD_RE.fullmatch(c) is not None
           and _ROMAN_RE.match(c) is None for c in core]
    for i, w in enumerate(words):
        if not isc[i]:
            continue
        run = (i and isc[i - 1]) or (i + 1 < len(isc) and isc[i + 1])
        if run or len(core[i]) <= 2:
            parts[i * 2] = w.lower()
    return "".join(parts)


@dataclass
class Block:
    kind: BlockKind
    text: str = ""
    italic: list[tuple[int, int]] = field(default_factory=list)
    meta: dict = field(default_factory=dict)   # kind-specific: chat -> {user, location}


def _under_1000(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    hundreds, rest = divmod(n, 100)
    return f"{_ONES[hundreds]} hundred" + (f" {_under_1000(rest)}" if rest else "")


def int_to_words(n: int) -> str:
    if n < 0:
        return "minus " + int_to_words(-n)
    if n == 0:
        return "zero"
    parts: list[str] = []
    for unit, name in ((1_000_000, "million"), (1000, "thousand"), (1, "")):
        if n >= unit:
            q, n = divmod(n, unit)
            parts.append(_under_1000(q) + (f" {name}" if name else ""))
    return " ".join(parts)


def _ordinal_words(num: str) -> str:
    if num in _ORDINAL_SMALL:
        return _ORDINAL_SMALL[num]
    words = int_to_words(int(num))
    if words.endswith("y"):
        return words[:-1] + "ieth"
    tail = {
        "one": "first", "two": "second", "three": "third", "five": "fifth",
        "eight": "eighth", "nine": "ninth", "twelve": "twelfth",
    }
    for k, v in tail.items():
        if words.endswith(k):
            return words[: -len(k)] + v
    return words + "th"


def _strip_zero_width(s: str) -> str:
    return s.translate(_ZERO_WIDTH)


_DEQUOTE_TABLE = {ord(k): v for k, v in _CURLY.items()}


def dequote_preserving(s: str) -> str:
    """Curly -> ASCII quotes, one char for one char (offsets stay valid)."""
    return s.translate(_DEQUOTE_TABLE)


def _dequote(s: str) -> str:
    return s.translate(_DEQUOTE_TABLE)


def _decimal_words(m: re.Match[str]) -> str:
    whole, frac = m.group(1), m.group(2)
    digits = " ".join(_ONES[int(d)] for d in frac)
    return f"{int_to_words(int(whole))} point {digits}"


def normalize_text(s: str, *, dampen_caps: bool = True) -> str:
    """Spoken-form normalization. Keeps `…` (TTS renders the trailing pause well)."""
    s = unicodedata.normalize("NFC", s)
    s = _strip_zero_width(s)
    s = _EMOJI_RE.sub(" ", s)
    s = _dequote(s)
    s = _MD_ITALIC_RE.sub(r"\1", s)
    if dampen_caps:
        s = _dampen_caps(s)
    s = _SPACED_ELLIPSIS_RE.sub("…", s)
    s = s.replace("...", "…")
    s = _THOUSANDS_RE.sub("", s)
    s = s.replace("—", " — ").replace("–", " – ").replace("--", " — ")
    for pattern, repl in _TEXT_SUBS:
        s = pattern.sub(repl, s)
    s = _HASHNUM_RE.sub(lambda m: f"number {int_to_words(int(m.group(1)))}", s)
    s = _PLUSMINUS_RE.sub(lambda m: " plus " if m.group(1) == "+" else " minus ", s)
    s = _LVL_RE.sub("level", s)
    s = _DECIMAL_RE.sub(_decimal_words, s)
    s = _ORD_RE.sub(lambda m: _ordinal_words(m.group(1)), s)
    s = _INT_RE.sub(lambda m: int_to_words(int(m.group(1))), s)
    s = _WS_RE.sub(" ", s)
    s = _SPACE_PUNCT_RE.sub(r"\1", s)
    return s.strip()


_CHAPTER_HEAD_RE = re.compile(r"^\s*(?:chapter\s+|ch\.?\s*)?(\d{1,4})\s*[.\-:)\]]*\s*(.*)$", re.I)


def normalize_heading(s: str) -> str:
    """Speak a chapter heading naturally: '1- Blade Of Old' -> 'Chapter One. Blade Of Old'."""
    m = _CHAPTER_HEAD_RE.match(s)
    if m:
        num = int_to_words(int(m.group(1))).capitalize()
        rest = m.group(2).strip(" .:-–—")
        return normalize_text(f"Chapter {num}" + (f". {rest}" if rest else ""))
    return normalize_text(s)


def normalize_username(handle: str) -> str:
    """A stream handle -> something speakable: 'Forever1stCommenter' -> 'Forever first Commenter'."""
    h = handle.strip().lstrip("@[").rstrip("]:").strip()
    h = _CAMEL_RE.sub(" ", h)
    h = _LETNUM_RE.sub(" ", h)
    h = re.sub(r"[_\-.]+", " ", h)
    h = re.sub(r"\b(\d{1,4}) (st|nd|rd|th)\b", r"\1\2", h, flags=re.I)  # "1 st" -> "1st"
    return normalize_text(h) or handle.strip()


def normalize_chat_message(s: str, *, dampen_caps: bool = True) -> str:
    s = s.lstrip("[").rstrip("]").strip()
    s = re.sub(r"(?<!\w)@(\w)", r"\1", s)          # drop @ from mentions
    if dampen_caps:
        s = _ALLCAPS_RE.sub(lambda m: m.group(1).capitalize(), s)   # lone long shouts
    return normalize_text(s, dampen_caps=dampen_caps)


_SYSTEM_SUBS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bHP\b"), "hit points"),
    (re.compile(r"\bMP\b"), "mana points"),
    (re.compile(r"\b(SP|EP)\b"), "stamina points"),
    (re.compile(r"\b(XP|EXP)\b"), "experience"),
    (re.compile(r"\bLv\.?\b|\bLvl\.?\b", re.I), "Level"),
    (re.compile(r"\bSTR\b"), "Strength"),
    (re.compile(r"\bDEX\b"), "Dexterity"),
    (re.compile(r"\bCON\b"), "Constitution"),
    (re.compile(r"\bINT\b"), "Intelligence"),
    (re.compile(r"\bWIS\b"), "Wisdom"),
    (re.compile(r"\bCHA\b"), "Charisma"),
    (re.compile(r"\bVIT\b"), "Vitality"),
    (re.compile(r"\bAGI\b"), "Agility"),
]
_RATIO_RE = re.compile(r"\b(\d{1,5})\s*/\s*(\d{1,5})\b")
_ARROW_RE = re.compile(r"\s*(?:->|=>|→|➔|➜)\s*")


def normalize_system(s: str) -> str:
    """Extra spoken-form rules for LitRPG status boxes / stat lines."""
    s = _ARROW_RE.sub(" to ", s)
    s = s.replace("[", " ").replace("]", " ")
    for pattern, repl in _SYSTEM_SUBS:
        s = pattern.sub(repl, s)
    s = _RATIO_RE.sub(
        lambda m: f"{int_to_words(int(m.group(1)))} out of {int_to_words(int(m.group(2)))}", s
    )
    return normalize_text(s, dampen_caps=False)   # keep stat-box acronyms (AC, DR, …)


def is_scene_break(line: str) -> bool:
    compact = line.strip()
    if compact in _SCENE_BREAK_LITERAL:
        return True
    return len(compact) <= 12 and bool(_SCENE_BREAK_RE.match(compact))


def load_blocks_from_text(raw: str) -> list[Block]:
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[Block] = []
    for chunk in re.split(r"\n\s*\n", raw):
        stripped = chunk.strip()
        if not stripped:
            continue
        if is_scene_break(stripped):
            blocks.append(Block("scene_break"))
            continue
        text = " ".join(line.strip() for line in stripped.splitlines() if line.strip())
        blocks.append(Block("paragraph", text))
    return blocks
