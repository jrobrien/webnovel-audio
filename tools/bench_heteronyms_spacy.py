"""Would a spaCy POS tag pick heteronym readings better than espeak does?

Companion to `bench_heteronyms_espeak.py`: same corpus, same sentences, so
the two numbers are directly comparable. Run the espeak one FIRST — this
reads its results.json for the head-to-head.

spaCy is NOT a project dependency and must not be installed into .venv
(that venv's phonemizer/espeak versions are part of the render fingerprint).
Use a throwaway one:

    uv venv --python 3.12 /tmp/spacyenv
    VIRTUAL_ENV=/tmp/spacyenv uv pip install spacy
    VIRTUAL_ENV=/tmp/spacyenv uv pip install \
      https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
    /tmp/spacyenv/bin/python tools/bench_heteronyms_spacy.py
"""
from __future__ import annotations

import os

# A render may be using the rest of the machine; stay on one core so the
# timings mean something and we don't steal from it.
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "BLIS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_v] = "1"

import collections
import csv
import glob
import json
import sys
import time

import spacy

ROOT = os.environ.get("HOMOGRAPH_DATA", "/tmp/homograph-data")
DATA = os.path.join(ROOT, "WikipediaHomographData-master", "data")
MODELS = os.environ.get("SPACY_MODELS", "en_core_web_sm,en_core_web_md").split(",")

# The four POS labels the dataset uses where the split really is grammatical.
POSLAB = {"verb", "noun", "adjective", "adjective-noun"}


def accepts(label: str, pos: str) -> bool:
    if label == "verb":
        return pos in ("VERB", "AUX")
    if label == "noun":
        return pos in ("NOUN", "PROPN")
    if label == "adjective":
        return pos == "ADJ"
    if label == "adjective-noun":
        return pos in ("ADJ", "NOUN", "PROPN")
    return False


def predict(refs, hom: str, pos: str, tag: str, lemma: str) -> str | None:
    """Which sense the tag implies, or None if it doesn't single one out.

    `read` and `wound` are the two cases coarse POS can't reach: both readings
    are verbs, and only the fine tag (and, for `wound`, the lemma) separates
    them. They are also the two that turn up most in narration.
    """
    if hom == "read":
        return "read_past" if tag in ("VBD", "VBN") else "read_present"
    if hom == "wound":
        return "wound_vrb" if tag in ("VBD", "VBN") and lemma == "wind" else "wound_nou-vrb"
    hits = [w for w, l in refs[hom].items() if accepts(l, pos)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        # An ADJ/ADV/NUM tag on a word whose senses are only verb|noun. The
        # nominal reading is right far more often than abstaining is (+3.2pp).
        nom = [w for w, l in refs[hom].items()
               if l in ("noun", "adjective-noun", "adjective")]
        if len(nom) == 1:
            return nom[0]
    return None


def main() -> None:
    refs: dict[str, dict[str, str]] = collections.defaultdict(dict)
    with open(os.path.join(DATA, "wordids.tsv")) as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            r = {k: v.strip('"') for k, v in r.items()}
            refs[r["homograph"]][r["wordid"]] = r["label"]

    scope = {w for w, s in refs.items()
             if all(l in POSLAB for l in s.values()) and len(set(s.values())) == len(s)}
    scope |= {"read", "wound"}

    items = []
    for split in ("train", "eval"):
        for path in sorted(glob.glob(f"{DATA}/{split}/*.tsv")):
            with open(path) as fh:
                for r in csv.DictReader(fh, delimiter="\t"):
                    r = {k: v.strip('"') for k, v in r.items()}
                    items.append((r["homograph"], r["wordid"], r["sentence"],
                                  int(r["start"]), int(r["end"])))

    espeak_path = os.path.join(ROOT, "results.json")
    espeak = json.load(open(espeak_path)) if os.path.exists(espeak_path) else None
    if espeak and len(espeak) != len(items):
        print("results.json is stale; re-run the espeak bench", file=sys.stderr)
        espeak = None

    scoped = [(i, it) for i, it in enumerate(items) if it[0] in scope]
    texts = [it[2] for _, it in scoped]
    nwords = sum(len(t.split()) for t in texts)
    print(f"{len(scope)} homographs, {len(scoped)} sentences, {nwords} words",
          file=sys.stderr)

    print(f"\n{'model':22s} {'acc':>7s} {'abstain':>8s} {'load':>7s} "
          f"{'cpu(s)':>8s} {'words/s':>9s} {'s/chapter':>10s}")
    for model in MODELS:
        t0, w0 = time.process_time(), time.perf_counter()
        # parser and NER cost most of the runtime and neither is used here;
        # the lemmatizer stays because `wound` needs it.
        nlp = spacy.load(model, exclude=["ner", "parser", "senter"])
        load = time.process_time() - t0

        t0 = time.process_time()
        docs = list(nlp.pipe(texts, batch_size=64))
        cpu = time.process_time() - t0

        rows = []
        for (idx, (hom, gold, sent, s, e)), doc in zip(scoped, docs):
            span = doc.char_span(s, e, alignment_mode="expand")
            tok = span[0] if span is not None and len(span) else None
            pred = (predict(refs, hom, tok.pos_, tok.tag_, tok.lemma_.lower())
                    if tok is not None else None)
            rows.append(dict(i=idx, hom=hom, gold=gold, pred=pred,
                             pos=tok.pos_ if tok else None,
                             tag=tok.tag_ if tok else None))
        n = len(rows)
        acc = sum(r["pred"] == r["gold"] for r in rows) / n
        ab = sum(r["pred"] is None for r in rows) / n
        wps = nwords / cpu
        print(f"{model:22s} {100*acc:6.1f}% {100*ab:7.1f}% {load:6.2f}s "
              f"{cpu:8.2f} {wps:9,.0f} {2800/wps:9.2f}s")

        with open(os.path.join(ROOT, f"spacy_{model}.json"), "w") as fh:
            json.dump(rows, fh)

        if espeak:
            # Compare only where the espeak bench aligned cleanly.
            ok = [r for r in rows if espeak[r["i"]]["pred"] and espeak[r["i"]]["fit"] <= 2]
            ea = sum(espeak[r["i"]]["pred"] == espeak[r["i"]]["gold"] for r in ok) / len(ok)
            sa = sum(r["pred"] == r["gold"] for r in ok) / len(ok)
            print(f"{'':22s} head-to-head on {len(ok)} aligned: "
                  f"espeak {100*ea:.1f}%  spaCy {100*sa:.1f}%")


if __name__ == "__main__":
    main()
