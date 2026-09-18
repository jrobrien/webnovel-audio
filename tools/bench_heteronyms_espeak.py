"""Score the render path's g2p on Google's WikipediaHomographData.

espeak-ng decides heteronym readings on its own, invisibly, and a routine
`uv sync` can change those decisions (see `synth/kokoro.py` fingerprinting).
This measures what it actually gets right, per word, so a change is
answerable rather than merely detectable.

Run with the PROJECT venv — it deliberately uses the same phonemizer call the
Kokoro backend makes, so the numbers describe real renders:

    .venv/bin/python tools/bench_heteronyms_espeak.py

Writes results.json next to the data. ~10 s for 16k sentences.
"""
from __future__ import annotations

import collections
import csv
import glob
import io
import json
import os
import sys
import tarfile
import unicodedata
import urllib.request

import espeakng_loader
import phonemizer
from phonemizer.backend.espeak.wrapper import EspeakWrapper
from phonemizer.separator import Separator

TARBALL = ("https://codeload.github.com/google-research-datasets/"
           "WikipediaHomographData/tar.gz/refs/heads/master")
ROOT = os.environ.get("HOMOGRAPH_DATA", "/tmp/homograph-data")
DATA = os.path.join(ROOT, "WikipediaHomographData-master", "data")


def fetch() -> None:
    """Grab the corpus once (Apache-2.0, ~3 MB). Not vendored: it's data, not code."""
    if os.path.isdir(DATA):
        return
    os.makedirs(ROOT, exist_ok=True)
    print(f"fetching corpus -> {ROOT}", file=sys.stderr)
    with urllib.request.urlopen(TARBALL) as fh:
        tarfile.open(fileobj=io.BytesIO(fh.read())).extractall(ROOT, filter="data")


# --- IPA comparison ---------------------------------------------------------
# espeak and the dataset use different transcription conventions, so neither
# raw string equality nor naive edit distance works. Normalize both sides.
STRESS = "ˈˌ'"
VOWELS = set("aeiouæɑɐɒəɚɜɛɪɔʊʌyøœɨʉɯɤɘɵɞɶᵻᵊ")
FOLD = {"ᵻ": "ɪ", "ᵊ": "ə", "ɐ": "ə", "ʔ": "", "r": "ɹ", "ɡ": "g"}


def canon(p: str) -> tuple[str, int]:
    """(phonemes without stress, index of the primary-stressed vowel or -1).

    espeak marks stress immediately before the stressed *vowel*; the dataset
    marks it before the *syllable*. The first vowel at or after the mark is
    the same vowel either way, so index that vowel rather than the mark —
    otherwise every stress-shift pair (CON-tent / con-TENT) miscompares.
    """
    p = unicodedata.normalize("NFD", p)
    p = "".join(FOLD.get(c, c) for c in p if unicodedata.category(c) != "Mn" or c == "ː")
    p = p.replace("ː", "")                          # length marks disagree
    p = p.replace("ɪɹ", "iɹ").replace("ʊɹ", "uɹ")   # espeak's NEAR / CURE
    seq, mark, vi, stressed = [], False, 0, -1
    for c in p:
        if c in STRESS:
            mark = c in "ˈ'"
            continue
        if not c.strip() or c in ".,;:!?-()\"":
            continue
        if c in VOWELS:
            if mark and stressed < 0:
                stressed = vi
            vi += 1
            mark = False
        seq.append(c)
    return "".join(seq), stressed


def lev(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def dist(a: str, b: str) -> int:
    (sa, va), (sb, vb) = canon(a), canon(b)
    return lev(sa, sb) + (2 if va != vb else 0)


def main() -> None:
    fetch()
    EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
    EspeakWrapper.set_library(espeakng_loader.get_library_path())

    refs: dict[str, dict[str, str]] = collections.defaultdict(dict)
    with open(os.path.join(DATA, "wordids.tsv")) as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            r = {k: v.strip('"') for k, v in r.items()}
            refs[r["homograph"]][r["wordid"]] = r["pronunciation"]

    items = []
    for split in ("train", "eval"):
        for path in sorted(glob.glob(f"{DATA}/{split}/*.tsv")):
            with open(path) as fh:
                for r in csv.DictReader(fh, delimiter="\t"):
                    r = {k: v.strip('"') for k, v in r.items()}
                    items.append((r["homograph"], r["wordid"], r["sentence"],
                                  int(r["start"]), split))
    print(f"{len(items)} sentences, {len(refs)} homographs", file=sys.stderr)

    sep = Separator(word="|", phone="")

    def ph(text: str) -> list[str]:
        if not text.strip():
            return []
        out = phonemizer.phonemize(text, "en-us", preserve_punctuation=True,
                                   with_stress=True, separator=sep)
        return [w for w in out.replace("\n", "|").split("|") if w]

    results = []
    for n, (hom, gold, sent, start, split) in enumerate(items):
        words = ph(sent)
        # Index the target by phonemizing the prefix: espeak merges tokens
        # ("do not" -> duːnˌɑːt), so its own count is the only reliable one.
        idx = len(ph(sent[:start]))
        tok = words[idx] if idx < len(words) else None
        if tok is None:
            results.append(dict(homograph=hom, gold=gold, pred=None, margin=None,
                                fit=None, phones=None, split=split))
            continue
        scored = sorted((dist(tok, p), w) for w, p in refs[hom].items())
        results.append(dict(homograph=hom, gold=gold, pred=scored[0][1],
                            margin=(scored[1][0] - scored[0][0]) if len(scored) > 1 else 99,
                            fit=scored[0][0], phones=tok, split=split))
        if n % 1000 == 0:
            print(f"\r{n}/{len(items)}", end="", file=sys.stderr)
    print(file=sys.stderr)

    out = os.path.join(ROOT, "results.json")
    with open(out, "w") as fh:
        json.dump(results, fh)

    # `fit` is the edit distance from the chosen token to its nearest reference;
    # a large one means the alignment missed, not that espeak was wrong.
    good = [r for r in results if r["pred"] and r["fit"] <= 2]
    acc = sum(r["pred"] == r["gold"] for r in good) / len(good)
    byw = collections.defaultdict(list)
    for r in good:
        byw[r["homograph"]].append(r)
    base = sum(collections.Counter(x["gold"] for x in rs).most_common(1)[0][1]
               for rs in byw.values()) / len(good)
    fixed = sum(1 for rs in byw.values() if len({r["pred"] for r in rs}) == 1)
    print(f"\naligned {len(good)}/{len(results)} ({100*len(good)/len(results):.1f}%)")
    print(f"espeak accuracy        : {100*acc:.1f}%")
    print(f"majority-class baseline: {100*base:.1f}%")
    print(f"words espeak never varies: {fixed}/{len(byw)}")
    print(f"\nper-sentence detail -> {out}")


if __name__ == "__main__":
    main()
