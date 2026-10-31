"""Find closed compounds the pronunciation front end may read wrongly.

espeak-ng reads a word it does not know by its own rules, and a made-up compound
(`hoverbus`, `nightshade`) is where those rules go wrong: a vowel is reduced, a
stress lands on the wrong half, or `t` + `sh` merge into `ch`. The check is
mechanical: a word that is not in a dictionary but splits into two words that are,
is phonemized whole and as its two halves, and flagged when the two disagree.
Everything is judged by the phonemes the voice would actually get, so a flagged word
is one worth listening to, not a guess about spelling.

Core module: it names no site and calls no network. The phonemizer is passed in.
"""
from __future__ import annotations

import difflib
import glob
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Iterable

_WORD_RE = re.compile(r"\b[A-Za-z]{6,}\b")
_ALL_RE = re.compile(r"\b[A-Za-z]{3,}\b")
_STRIP_RE = re.compile(r"[ˈˌː ]")
_INFLECTION_RE = re.compile(r"(?:ing|ed|es|s|ly|er|ers|est|ist|ists|ier|ies|ie|ate|ity|ities)$")
# endings that are read weakly by design: `-less` is not a second word to compare against
_SUFFIXES = {"less", "able", "ible", "ness", "ment", "ship", "hood", "ful", "fully", "ward",
             "wards", "wise", "ish", "ling", "ings", "man", "men", "land", "worth", "ways", "time"}
# two sounds that fuse into one when a compound is spoken as a single word
_FUSES = {("t", "ʃ"): "t+sh -> ch", ("d", "ʒ"): "d+zh -> j", ("t", "j"): "t+y -> ch",
          ("d", "j"): "d+y -> j"}


@dataclass
class Finding:
    word: str
    count: int
    parts: tuple[str, str]
    whole: str                  # phonemes of the word as written
    split: str                  # phonemes of the two halves read as separate words
    why: str                    # "differs" | "joins" | "ok"
    where: list[str] = field(default_factory=list)   # `slug#N` of the first few uses


def load_dictionary() -> set[str]:
    """Lower-case words from the system word list (cracklib or `words`)."""
    for path in sorted(glob.glob("/usr/share/dict/*")):
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                words = {w.strip().lower() for w in fh if w.strip().isalpha()}
        except OSError:
            continue
        if len(words) > 20000:
            return words
    raise SystemExit("compounds: no system word list found in /usr/share/dict "
                     "(install cracklib or a `words` package)")


def _bare(p: str) -> str:
    return _STRIP_RE.sub("", p)


def _is_inflection(word: str, dictionary: set[str]) -> bool:
    """`flatten`, `pinning`, `waved`: a dictionary word plus an ending, not a compound."""
    m = _INFLECTION_RE.search(word)
    if not m:
        return False
    stem = word[:m.start()]
    return any(c in dictionary for c in (stem, stem + "e", stem[:-1], stem[:-1] + "y"))


_VOWELS = set("aeiouɐəɚɪɛʌʊɔɑæɜᵻɒɑ")


def classify(parts: str, whole: str) -> str:
    """What kind of difference separates the halves read apart from the word read whole:
    `n→ng` (`skincare`: the n slides to ng before k/g), `s/z`, `vowel` (a half is weakened:
    `hoverbus` -> `bəs`) or `differs` (anything else)."""
    sm = difflib.SequenceMatcher(None, parts, whole, autojunk=False)
    pairs = [(parts[i1:i2], whole[j1:j2])
             for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]
    if pairs and all(a == "n" and b == "ŋ" for a, b in pairs):
        return "n→ng"
    if pairs and all(set(a + b) <= {"s", "z"} for a, b in pairs):
        return "s/z"
    if pairs and all(set(a + b) <= _VOWELS for a, b in pairs):
        return "vowel"
    return "differs"


def judge(word: str, a: str, b: str, phonemize: Callable[[str], str]) -> tuple[str, str, str, str]:
    """(why, whole phonemes, split phonemes) for one split of `word` into `a` + `b`."""
    whole, pa, pb = phonemize(word), phonemize(a), phonemize(b)
    split = f"{pa} {pb}"
    bw, ba, bb = _bare(whole), _bare(pa), _bare(pb)
    if bw != ba + bb:
        return classify(ba + bb, bw), whole, split, ""
    if ba and bb and (ba[-1], bb[0]) in _FUSES:
        return "joins", whole, split, _FUSES[(ba[-1], bb[0])]
    return "ok", whole, split, ""


def scan(texts: Iterable[tuple[str, str]], phonemize: Callable[[str], str],
         dictionary: set[str], *, known: set[str] = frozenset(), min_count: int = 1,
         min_part: int = 3, part_uses: int = 40,
         vocabulary: Iterable[tuple[str, str]] | None = None) -> list[Finding]:
    """Compounds in `texts` ((label, text) pairs), most-used flagged ones first.

    A candidate is a word of 6+ letters that is not in `dictionary`, not an inflected
    dictionary word and not already ruled (`known`), which splits into two halves that
    are each dictionary words used at least `part_uses` times in the texts themselves
    (so `bus` counts but a chance fragment does not). `vocabulary` is where those uses
    are counted when that should be wider than `texts` (a range, one series)."""
    phonemize = lru_cache(maxsize=None)(phonemize)
    freq: Counter[str] = Counter()
    uses: dict[str, Counter] = defaultdict(Counter)
    docs = list(texts)
    for _, text in (docs if vocabulary is None else vocabulary):
        for m in _ALL_RE.finditer(text):
            freq[m.group(0).lower()] += 1
    parts = {w for w, n in freq.items() if n >= part_uses and w in dictionary}
    for label, text in docs:
        for m in _WORD_RE.finditer(text):
            w = m.group(0).lower()
            if w in dictionary or w in known or _is_inflection(w, dictionary):
                continue
            uses[w][label] += 1

    out: list[Finding] = []
    for w, labels in uses.items():
        n = sum(labels.values())
        if n < min_count:
            continue
        best = None
        # the most even split first: `night+shade`, not `nigh+tshade`; a 3-letter half
        # (`bus`) is allowed only against a long one
        for i in sorted(range(min_part, len(w) - min_part + 1),
                        key=lambda i: -min(i, len(w) - i)):
            a, b = w[:i], w[i:]
            if min(len(a), len(b)) < 4 and max(len(a), len(b)) < 5:
                continue
            if a in parts and b in parts and b not in _SUFFIXES:
                why, whole, split, note = judge(w, a, b, phonemize)
                cand = Finding(w, n, (a, b), whole, split, why if not note else f"joins ({note})",
                               [f"{k}" for k, _ in labels.most_common(4)])
                if best is None:                         # the most even split decides
                    best = cand
        if best:
            out.append(best)
    return sorted(out, key=lambda f: (f.why == "ok", -f.count, f.word))
