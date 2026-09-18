"""Pronunciation rules: one table, one resolver, used by every path.

There used to be two systems — a literal find-and-replace lexicon and a
separate POS-keyed heteronym table — applied one after the other with the
lexicon barred from what the tagger had touched. Two formats, two precedence
rules, and a whole class of bug where a word was pinned for one part of speech
and left to espeak for the others. `live` shipped wrong exactly that way.

Now there is one CSV and one resolution order. A rule may name a part of
speech or not:

    surface,pos,respell,notes
    Montgomery,,mahnt-gum-uh-ree,name the g2p mangles
    live,ADJ,lyve,/laɪv/
    live,VERB,liv,/lɪv/
    read,VBD,red,past tense
    wound,VBD+wind,wownd,past of wind, not an injury
    a tear in,,a tair in,the cloth sense

An empty `pos` means "whatever the tagger says" — the default reading, and
what a multi-word phrase always uses. Matching is case-insensitive and the
original word's capitalization is carried onto the respelling, so `Chi` and
`chi` no longer need separate rows.

Most specific wins:

    1. a multi-word phrase
    2. a fine Penn tag with a lemma constraint  (wound/VBD+wind)
    3. a fine Penn tag                          (read/VBD)
    4. a coarse part of speech                  (live/VERB)
    5. no part of speech                        (Montgomery)

and at equal specificity a per-series file beats the base file. A row with an
empty `respell` is a deliberate no-op: "I listened, it reads fine" — it makes
the word count as known so `check` stops suggesting it.
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass

#: Coarse spaCy POS values, as opposed to fine Penn tags. A rule's `pos` is
#: read as coarse if it appears here, and as a fine tag otherwise.
COARSE = {"NOUN", "PROPN", "VERB", "AUX", "ADJ", "ADV", "NUM", "INTJ", "PART",
          "PRON", "DET", "ADP", "CCONJ", "SCONJ"}

#: Tags meaning "not a verb here" for a word whose only rules are nominal.
#: Attributive nouns get tagged ADJ often enough that abstaining costs real
#: accuracy — falling back to the noun rule was worth +3.2 points.
NOMINAL = {"ADJ", "ADV", "NUM", "PROPN"}

HEADER = "surface,pos,respell,notes\n"

ANY = ""          # the empty `pos`: applies whatever the tag


@dataclass(frozen=True)
class Rule:
    surface: str
    pos: str = ANY
    respell: str = ""
    notes: str = ""
    lemma: str = ""

    @classmethod
    def parse(cls, surface: str, pos: str = "", respell: str = "",
              notes: str = "") -> "Rule":
        """Build a rule, splitting `TAG+lemma` out of the `pos` field."""
        pos, _, lemma = pos.strip().partition("+")
        return cls(surface=surface.strip(), pos=pos.strip(),
                   respell=respell.strip(), notes=notes.strip(),
                   lemma=lemma.strip().lower())

    @property
    def is_phrase(self) -> bool:
        return " " in self.surface.strip()

    @property
    def specificity(self) -> int:
        if self.is_phrase:
            return 4
        if not self.pos:
            return 0
        if self.pos in COARSE:
            return 1
        return 3 if self.lemma else 2


def _uncommented(lines):
    """Drop `#` comment lines before the CSV reader sees them.

    CSV has no comment convention and `csv` has no option for one. It matters
    most for the *first* line: a comment there would be taken as the header,
    making every lookup miss and silently voiding the whole file.
    """
    return (ln for ln in lines if not ln.lstrip().startswith("#"))


def match_case(original: str, respell: str) -> str:
    """Carry a leading capital from the source word onto the respelling.

    Only the leading capital. Upper-casing a whole respelling would be worse
    than useless: espeak reads an all-caps token as individual letter names, so
    a shouted "LIVE" pinned to "liv" would come out "ell eye vee" instead of
    /lɪv/. Capitalization is cosmetic for the TTS either way — the reader-facing
    Markdown is written from the original blocks, never from respelled text —
    so the safe choice is the quiet one.
    """
    if not respell:
        return respell
    if original[:1].isupper():
        return respell[:1].upper() + respell[1:]
    return respell


class Lexicon:
    def __init__(self, rules: list[Rule]):
        self.rules = rules
        self.tokens: dict[str, list[Rule]] = {}
        phrases: list[Rule] = []
        for r in rules:
            if r.is_phrase:
                phrases.append(r)
            else:
                self.tokens.setdefault(r.surface.lower(), []).append(r)
        # Longest first so a phrase beats its own substrings, and one pass so a
        # replacement can never be re-matched by a later rule.
        self.phrases = sorted(phrases, key=lambda r: -len(r.surface))
        self._phrase_rx = None
        live = [r for r in self.phrases if r.respell]
        if live:
            alts = "|".join(re.escape(r.surface) for r in live)
            self._phrase_rx = re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.I)
            self._phrase_by = {r.surface.lower(): r for r in live}

    # -- resolution ---------------------------------------------------------

    def for_token(self, text: str, pos: str, tag: str, lemma: str) -> Rule | None:
        """The rule that applies to one tagged word, or None."""
        cands = self.tokens.get(text.lower())
        if not cands:
            return None
        lemma = (lemma or "").lower()
        best = None
        for r in cands:
            if r.lemma and not (r.pos == tag and r.lemma == lemma):
                continue
            if not r.lemma and r.pos and r.pos not in COARSE and r.pos != tag:
                continue
            if not r.lemma and r.pos in COARSE and r.pos != pos:
                continue
            if best is None or r.specificity > best.specificity:
                best = r
        if best is None and pos in NOMINAL:
            # An ADJ/ADV/NUM tag on a word whose rules are only nominal.
            nom = [r for r in cands if r.pos in ("NOUN", "PROPN")]
            if len(nom) == 1:
                best = nom[0]
        return best

    def apply(self, text: str, nlp=None) -> str:
        """Rewrite `text` into what the TTS should say.

        `nlp` is a loaded spaCy pipeline. Without one only phrase rules and
        `pos`-less rules can fire, which is enough for tooling that just wants
        to preview substitutions; the render path always passes one.
        """
        if not text.strip():
            return text
        edits: list[tuple[int, int, str]] = []
        taken: list[tuple[int, int]] = []

        if self._phrase_rx is not None:
            for m in self._phrase_rx.finditer(text):
                rule = self._phrase_by.get(m.group(0).lower())
                if rule:
                    edits.append((m.start(), m.end(),
                                  match_case(m.group(0), rule.respell)))
                    taken.append(m.span())

        if self.tokens:
            for tok_text, start, pos, tag, lemma in self._words(text, nlp):
                if any(a <= start < b for a, b in taken):
                    continue
                rule = self.for_token(tok_text, pos, tag, lemma)
                if rule and rule.respell:
                    edits.append((start, start + len(tok_text),
                                  match_case(tok_text, rule.respell)))

        if not edits:
            return text
        edits.sort()
        out, last = [], 0
        for start, end, repl in edits:
            if start < last:
                continue                      # overlapping: first (longest) wins
            out.append(text[last:start])
            out.append(repl)
            last = end
        out.append(text[last:])
        return "".join(out)

    @staticmethod
    def _words(text: str, nlp):
        """(word, offset, pos, tag, lemma) for each word in `text`.

        With no tagger the pos/tag/lemma are blank, so only `pos`-less rules
        can match — a degraded preview, never the render path.
        """
        if nlp is None:
            for m in re.finditer(r"[^\W\d_]+(?:['’][^\W\d_]+)*", text):
                yield m.group(0), m.start(), "", "", ""
            return
        for tok in nlp(text):
            if tok.is_space or tok.is_punct:
                continue
            yield tok.text, tok.idx, tok.pos_, tok.tag_, (tok.lemma_ or "")

    # -- loading ------------------------------------------------------------

    def surfaces(self) -> set[str]:
        out: set[str] = set()
        for r in self.rules:
            out.add(r.surface)
            out.add(r.surface.rstrip("'s"))
        return out

    def covered(self) -> set[str]:
        """Surfaces that have at least one part-of-speech rule."""
        return {r.surface.lower() for r in self.rules if r.pos}

    @classmethod
    def load(cls, path: str) -> "Lexicon":
        rules: list[Rule] = []
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(_uncommented(fh)):
                surface = (row.get("surface") or "").strip()
                if not surface:
                    continue
                rules.append(Rule.parse(surface, row.get("pos") or "",
                                        row.get("respell") or "",
                                        row.get("notes") or ""))
        return cls(rules)

    @classmethod
    def load_many(cls, paths: list[str]) -> "Lexicon":
        """Merge several CSVs; a later file wins for the same (surface, pos).

        Used to stack a per-series file over the always-on base, so a series
        can refine one part of speech without disturbing the rest.
        """
        merged: dict[tuple[str, str, str], Rule] = {}
        for path in paths:
            if not path or not os.path.exists(path):
                continue
            for r in cls.load(path).rules:
                merged[(r.surface.lower(), r.pos, r.lemma)] = r
        return cls(list(merged.values()))

    # -- files --------------------------------------------------------------

    @staticmethod
    def starter_text(slug: str, base_lexicon: str = "") -> str:
        base = base_lexicon or "data/lexicons/_base.csv"
        return (
            f"# {slug} — pronunciation rules for this series.\n"
            f"# Hand-edited; `check {slug}` suggests candidates. Rows here beat\n"
            f"# {base}, which is always applied underneath.\n"
            "#\n"
            "#   surface   the word or phrase as it appears (case-insensitive)\n"
            "#   pos       when to apply it: blank = always; else a spaCy POS\n"
            "#             (NOUN VERB ADJ ...), a Penn tag (VBD VBN ...), or\n"
            "#             TAG+lemma. More specific wins.\n"
            "#   respell   plain-English respelling fed to the TTS\n"
            "#   notes     free text for your own reference\n"
            "#\n"
            "# Pin one sense of a heteronym by its grammar:\n"
            "#   live,VERB,liv         'will he live?'\n"
            "#   live,ADJ,lyve         'a live branch'\n"
            "# or by context, with a phrase (phrases ignore `pos`):\n"
            '#   a tear in,,a tair in\n'
            "#\n"
            "# An empty respell means 'I listened, it reads fine' and stops\n"
            "# `check` suggesting the word again.\n"
            "#\n"
            "# Quote any field containing a comma.\n"
            + HEADER)

    @staticmethod
    def append_candidates(path: str, names: list[str]) -> int:
        """Append blank rows for names not already present. Returns count added."""
        existing: set[str] = set()
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as fh:
                rows = list(csv.reader(_uncommented(fh)))
            existing = {r[0].strip().lower() for r in rows[1:] if r and r[0].strip()}
        new = [n for n in dict.fromkeys(names) if n.lower() not in existing]
        if not new:
            return 0
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        write_header = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            if write_header:
                fh.write(HEADER)
            # LF, not csv's default CRLF: these are hand-edited on Linux and
            # the starter header is LF, so a CRLF append would mix endings.
            writer = csv.writer(fh, lineterminator="\n")
            for name in new:
                writer.writerow([name, "", "", "candidate — verify by ear"])
        return len(new)
