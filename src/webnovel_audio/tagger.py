"""The part-of-speech tagger the pronunciation rules are resolved against.

This module is only the spaCy plumbing — loading a model, saying whether one
is installed. The rules and the resolution order live in `lexicon.py`, because
that is what they are: pronunciation rules that happen to name a part of
speech.

Tagging is required to render. espeak decides heteronyms on its own otherwise,
and it is measurably worse than a coin-weighted guess at it: 74.4% against an
84.8% majority-class baseline on Google's WikipediaHomographData, and for 85 of
155 words it never varies its answer at all. Tagging lifts that to ~96%. See
`docs/plans/heteronym-disambiguation.md`.
"""
from __future__ import annotations

#: Models we know how to install, smallest first. `sm` measured 96.2% and `md`
#: 97.0% on the same corpus — 0.8 points for 3.7x the disk — so `sm` is the
#: default and `md` is there for anyone who wants to pay for it.
KNOWN_MODELS = ("en_core_web_sm", "en_core_web_md")
DEFAULT_MODEL = "en_core_web_sm"


def available(model: str = DEFAULT_MODEL) -> bool:
    """Is spaCy importable and `model` installed? Never raises."""
    try:
        import importlib.util

        if importlib.util.find_spec("spacy") is None:
            return False
        return importlib.util.find_spec(model.replace("-", "_")) is not None
    except (ImportError, ValueError):
        return False


def load(model: str = DEFAULT_MODEL):
    """A loaded spaCy pipeline, or None if spaCy or the model is missing.

    The parser and NER are excluded: they are most of the runtime and neither
    is used. The lemmatizer stays, because `wound` is resolved by lemma.
    """
    if not available(model):
        return None
    try:
        import spacy

        return spacy.load(model, exclude=["ner", "parser", "senter"])
    except (ImportError, OSError):
        return None


def describe(model: str = DEFAULT_MODEL) -> str:
    """One line for `check` and `tagger status`, so the state is never a mystery."""
    if not available(model):
        return f"tagger: NOT INSTALLED ({model}) — rendering will refuse"
    return f"tagger: {model}"
