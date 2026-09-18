"""Find heteronyms where espeak is guessing, and say which rule is missing.

The lexicon only pins the readings someone thought to pin. Wherever a
rule is absent the tagger declines and espeak decides for itself — silently, and
not always the same way twice. That is how `live` as a verb shipped wrong: a
rule existed for the adjective, the verb was left to espeak, and espeak flips to
/laɪv/ under subject-auxiliary inversion ("Will he live?") while getting "he
will live" right.

The detector needs no reference data. For these words the part of speech
*determines* the reading, so if one (surface, POS) class comes out with more
than one pronunciation across the library, espeak is guessing and that class
wants a rule. Uncovered classes are checked against the covered ones too: a
VERB voiced identically to the word's ADJ rule is a bug even when it is
consistently wrong.

This is the real guard against one-sided rules; a static check cannot tell a
deliberate omission (espeak reads the other side correctly) from an oversight.

Run with the PROJECT venv, after `tagger install`:

    .venv/bin/python tools/audit_heteronym_rules.py [--all-surfaces]
"""
from __future__ import annotations

import argparse
import collections
import glob
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import espeakng_loader                                    # noqa: E402
import phonemizer                                         # noqa: E402
from phonemizer.backend.espeak.wrapper import EspeakWrapper   # noqa: E402
from phonemizer.separator import Separator                # noqa: E402

from webnovel_audio import pipeline as P                  # noqa: E402
from webnovel_audio.config import Config                  # noqa: E402
from webnovel_audio.segment import build_segments         # noqa: E402
from webnovel_audio.lexicon import Lexicon                # noqa: E402

SEP = Separator(word="|", phone="")
VOWELS = set("aeiouæɑɐɒəɚɜɛɪɔʊʌyøœɨʉɯɤɘɵɞɶᵻᵊ")


def canon(p: str) -> str:
    """A stress-aware key for one word's phonemes.

    Stress must survive: half these words differ *only* by which syllable
    carries it (IM-port against im-PORT), so a stress-blind comparison would
    call every stress-shift pair identical and report nothing. The mark is
    recorded as the index of the vowel it falls on rather than its string
    position, which is stable across transcription conventions.
    """
    p = p.split(".")[0].strip("…\"'()[],;:!?")      # espeak spells punctuation out
    seq, mark, vi, stressed = [], False, 0, -1
    for c in p:
        if c in "ˈ'":
            mark = True
            continue
        if c == "ˌ":
            continue
        if c in VOWELS:
            if mark and stressed < 0:
                stressed = vi
            vi += 1
            mark = False
        seq.append(c)
    return f"{''.join(seq)}@{stressed}"


def phones(text: str) -> list[str]:
    if not text.strip():
        return []
    out = phonemizer.phonemize(text, "en-us", preserve_punctuation=True,
                               with_stress=True, separator=SEP)
    return [w for w in out.replace("\n", "|").split("|") if w]


def word_phones(word: str) -> str:
    """The word phonemized alone — used only to sanity-check alignment."""
    out = phones(word)
    return canon(out[0]) if out else ""


def token_phones(text: str, start: int) -> str | None:
    """espeak's phonemes for the word at character offset `start`.

    Indexed by phonemizing the prefix: espeak merges tokens ("do not" ->
    duːnˌɑːt), so its own word count is the only one that lines up.
    """
    idx = len(phones(text[:start]))
    words = phones(text)
    if idx >= len(words):
        return None
    return canon(words[idx])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-surfaces", action="store_true",
                    help="also audit heteronyms.py words that have no rule yet")
    ap.add_argument("--library", default="library")
    args = ap.parse_args()

    EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
    EspeakWrapper.set_library(espeakng_loader.get_library_path())

    cfg = Config.load("config.toml")
    nlp = P._load_nlp(cfg)
    lex = P._load_lexicon(cfg)
    if lex is None:
        sys.exit("no lexicon to audit")
    rules: dict[str, list] = lex.tokens

    targets = set(rules)
    if args.all_surfaces:
        from webnovel_audio.heteronyms import HETERONYMS

        targets |= {w.lower() for w in HETERONYMS}
    if not targets:
        sys.exit("no rules to audit")
    word_rx = re.compile(r"\b(" + "|".join(sorted(map(re.escape, targets))) + r")\b", re.I)

    # (surface, POS) -> reading -> [(example, chapter)]
    seen: dict[tuple[str, str], dict[str, list]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    files = sorted(glob.glob(f"{args.library}/*/chapters/*.md"))
    for n, f in enumerate(files):
        blocks, _, _ = P.load_document(f, cfg)
        # Tag with the real tagger but phonemize the text the renderer would
        # actually send, so a rule that already fires is simply absent here.
        for seg in build_segments(blocks, cfg, lex, nlp):
            if not word_rx.search(seg.text):
                continue
            doc = nlp(seg.text)
            for tok in doc:
                low = tok.text.lower()
                if low not in targets:
                    continue
                # Ask the tagger itself whether a rule already decides this
                # token. Comparing coarse POS against the rule's `pos` column
                # would call `read,VBD` uncovered, because that rule keys on a
                # fine tag.
                if lex.for_token(tok.text, tok.pos_, tok.tag_, tok.lemma_) is not None:
                    continue
                ph = token_phones(seg.text, tok.idx)
                if not ph:
                    continue
                # Alignment is by word index and slips on hyphenated or
                # contracted neighbours ("re-read"), landing on the wrong
                # token. A reading that does not even start with the word's
                # own first phoneme is such a slip, not a pronunciation.
                solo = word_phones(low)
                if solo and ph[:1] != solo[:1]:
                    continue
                here = max(0, tok.idx - 34)
                seen[(low, tok.pos_)][ph].append(
                    ("…" + seg.text[here:tok.idx + 36].strip().replace("\n", " "),
                     os.path.basename(f)))
        if n % 25 == 0:
            print(f"\r{n}/{len(files)}", end="", file=sys.stderr)
    print(f"\r{len(files)} chapters scanned", file=sys.stderr)

    # What each surface's rules pin it to, phonemically — an uncovered class
    # voiced the same way is being read as the wrong part of speech.
    pinned_reading: dict[str, set[str]] = collections.defaultdict(set)
    for surface, rs in rules.items():
        for r in rs:
            if r.respell:
                pinned_reading[surface].add(word_phones(r.respell))

    problems = []
    for (surface, pos), readings in sorted(seen.items()):
        total = sum(len(v) for v in readings.values())
        if len(readings) > 1:
            problems.append(("INCONSISTENT", surface, pos, readings, total))
        elif set(readings) & pinned_reading.get(surface, set()):
            # voiced the same as another POS that *is* pinned: consistently wrong
            problems.append(("COLLIDES", surface, pos, readings, total))

    if not problems:
        print("\nno uncovered (surface, POS) class is ambiguous or colliding.")
        return
    print(f"\n{len(problems)} class(es) where espeak is deciding and should not be:\n")
    for kind, surface, pos, readings, total in sorted(problems, key=lambda p: -p[4]):
        print(f"  {surface}/{pos}  x{total}  [{kind}]")
        for ph, examples in sorted(readings.items(), key=lambda kv: -len(kv[1])):
            print(f"     {ph:<14} x{len(examples):<4} e.g. {examples[0][0]}")
            print(f"     {'':<14}      ({examples[0][1]})")
        print(f"     -> add a rule:  {surface},{pos},<respell>,\"...\"\n")


if __name__ == "__main__":
    main()
