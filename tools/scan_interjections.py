"""Find interjections in the library that espeak spells out as letter names.

Heteronyms are *ambiguous within* espeak's vocabulary; interjections fall
*outside* it. A vowel-less token like `mmm` or `shh` hits no g2p rule, so
espeak falls back to reading the letters aloud: "EM EM EM", "ESS AITCH
AITCH" — maximally jarring, because these only ever occur in dialogue.

Run with the PROJECT venv (it phonemizes through the render path's espeak):

    .venv/bin/python tools/scan_interjections.py [--all]

Default output is only the forms espeak gets wrong. `--all` lists every
interjection-like token found, including the ones it already handles.
"""
from __future__ import annotations

import argparse
import collections
import glob
import os
import re
import sys

import espeakng_loader
import phonemizer
from phonemizer.backend.espeak.wrapper import EspeakWrapper

LIB = os.environ.get("WEBNOVEL_LIBRARY", "library")
WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")
REPEAT = re.compile(r"(.)\1{2,}")                 # same letter three times over
VOWELS = set("aeiouy")

# Consonant-only tokens that are initialisms, not noises. espeak reads these
# as letters and is *right* to; excluding them keeps the report honest.
ABBR = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "pc", "tv", "th", "nd",
        "rd", "ld", "hp", "mp", "hq", "pvp", "npc", "xp", "dps", "cj", "jj",
        "tj", "dj", "bb", "gg", "ff"}

# Shapes interjections actually take in prose, written as the whole family so
# `ah`, `ahh` and `aaah` all land in one bucket.
FAMILIES = [
    ("hm", r"^h+m+p*f*h*$"), ("mm", r"^m+h*$"), ("ah", r"^a+h+$"),
    ("oh", r"^o+h+$|^o+o+h+$"), ("uh", r"^u+h+$"), ("eh", r"^e+h+$"),
    ("er", r"^e+r+m*$"), ("um", r"^u+m+$"), ("ugh", r"^u+g+h+$"),
    ("argh", r"^a+r+g+h+$"), ("tsk", r"^t+s+k+$|^t+c+h+$"),
    ("pff", r"^p+f+t*$|^p+s+s+t+$"), ("grr", r"^g+r+$|^b+r+r+$"),
    ("shh", r"^s+h+$|^s+s+h+$"), ("huh", r"^h+u+h+$"),
    ("heh", r"^h+e+h+$|^h+a+h+$"), ("ow", r"^o+w+$|^o+o+f+$"),
    ("nng", r"^n+g+h*$|^n+n+h*$"), ("gah", r"^g+a+h+$|^b+a+h+$"),
    ("whoa", r"^w+h+o+a+$"), ("aw", r"^a+w+$"), ("ew", r"^e+w+$"),
    ("yay", r"^y+a+y+$"),
]
FAMILIES = [(n, re.compile(p)) for n, p in FAMILIES]

# espeak's phonemes for spoken letter names. Two or more of these in a short
# token means it gave up and spelled the word out.
# `iː` is deliberately absent: it is the letter E, but far more often just a
# long vowel (`eeeh` -> ˈiː is fine), so including it only produces noise.
LETTER_NAMES = ("eɪtʃ", "ɛm", "ɛn", "ɛs", "ɛf", "ɛl", "ɛks", "biː", "siː",
                "diː", "dʒeɪ", "keɪ", "ɑːɹ", "tiː", "viː", "dʌbəljuː",
                "waɪ", "zɛd", "piː", "kjuː", "dʒiː")


def family(word: str) -> str | None:
    core = word.strip("'-")
    if core.endswith("'s"):                 # CJ's is a possessive initialism
        core = core[:-2]
    if not core or core in ABBR:
        return None
    for name, pat in FAMILIES:
        if pat.match(core):
            return name
    if REPEAT.search(core):
        return "stretched"
    if len(core) >= 3 and not (set(core) & VOWELS):
        return "vowelless"
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="include forms espeak already handles correctly")
    args = ap.parse_args()

    EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
    EspeakWrapper.set_library(espeakng_loader.get_library_path())

    counts: collections.Counter[str] = collections.Counter()
    fams: dict[str, str] = {}
    chapters: dict[str, set[str]] = collections.defaultdict(set)
    series_ch: collections.Counter[str] = collections.Counter()
    ctx: dict[str, str] = {}
    total = 0

    paths = sorted(glob.glob(f"{LIB}/*/chapters/*.md"))
    if not paths:
        sys.exit(f"no chapters under {LIB}/ (set WEBNOVEL_LIBRARY)")
    for path in paths:
        text = open(path, encoding="utf-8").read()
        if text.startswith("---"):                       # drop YAML frontmatter
            text = text.split("\n---\n", 1)[-1]
        series = os.path.relpath(path, LIB).split(os.sep)[0]
        series_ch[series] += 1
        for m in WORD.finditer(text):
            total += 1
            w = m.group(0).lower().replace("’", "'")
            fam = family(w)
            if not fam:
                continue
            counts[w] += 1
            fams[w] = fam
            chapters[w].add(path)
            ctx.setdefault(w, re.sub(r"\s+", " ", text[max(0, m.start()-55):m.end()+25]))

    def phones(w: str) -> str:
        return phonemizer.phonemize(w, "en-us", preserve_punctuation=True,
                                    with_stress=True).strip()

    rows = []
    for w, n in counts.items():
        p = phones(w)
        spelled = sum(1 for l in LETTER_NAMES if l in p)
        bad = (not p) or spelled >= 2 or (spelled >= 1 and len(w) <= 4)
        rows.append((n, w, fams[w], p, bad))
    rows.sort(key=lambda r: -r[0])

    shown = [r for r in rows if args.all or r[4]]
    broken = sum(n for n, _, _, _, bad in rows if bad)
    print(f"{len(paths)} chapters, {total:,} words, "
          f"{sum(counts.values()):,} interjection-like tokens "
          f"({len(counts)} spellings)")
    print(f"espeak spells out as letters: {broken} occurrences, "
          f"{sum(1 for r in rows if r[4])} spellings\n")
    print(f"{'n':>4s} {'spelling':18s} {'family':10s} {'espeak says':34s} verdict")
    for n, w, fam, p, bad in shown:
        print(f"{n:4d} {w:18s} {fam:10s} {p:34s} {'LETTERS' if bad else 'ok'}")

    badwords = {w for _, w, _, _, bad in rows if bad}
    seen: dict[str, set[str]] = collections.defaultdict(set)
    for w in badwords:
        for c in chapters[w]:
            seen[os.path.relpath(c, LIB).split(os.sep)[0]].add(c)
    print(f"\n{'series':22s} {'chapters':>9s} {'affected':>9s}")
    for name in sorted(series_ch, key=lambda s: -series_ch[s]):
        print(f"{name:22s} {series_ch[name]:9d} {len(seen[name]):9d}")

    print("\ncontext for the worst offenders:")
    for n, w, fam, p, bad in shown[:8]:
        print(f"  {w:14s} …{ctx[w][-68:]}")


if __name__ == "__main__":
    main()
