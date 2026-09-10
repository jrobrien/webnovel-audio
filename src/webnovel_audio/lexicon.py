"""Per-series pronunciation dictionary.

CSV columns: surface,respell,ipa,notes
  surface : the word as it appears in the text (case-sensitive, whole-word match)
  respell : plain-English respelling fed to the TTS in place of `surface`
            (works with any backend; this is what Phase 1 uses)
  ipa     : optional IPA, reserved for backends that accept explicit phonemes
  notes   : free text for your own reference

Workflow: seed rows with an LLM's guesses, then fix `respell` by ear once.
`webnovel-audio inspect` lists capitalized words that are NOT yet in the lexicon.
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass


@dataclass
class Entry:
    surface: str
    respell: str = ""
    ipa: str = ""
    notes: str = ""


class Lexicon:
    def __init__(self, entries: list[Entry]):
        self.entries = entries
        self._subs = [
            (re.compile(rf"\b{re.escape(e.surface)}\b"), e.respell)
            for e in entries
            if e.respell and e.respell != e.surface
        ]

    @classmethod
    def load_many(cls, paths: list[str]) -> "Lexicon":
        """Merge several CSVs; a later file's row wins for the same surface.

        Used to stack the always-on base lexicon under a per-series one.
        """
        merged: dict[str, Entry] = {}
        for path in paths:
            if not path or not os.path.exists(path):
                continue
            for e in cls.load(path).entries:
                merged[e.surface] = e
        return cls(list(merged.values()))

    @classmethod
    def load(cls, path: str) -> "Lexicon":
        entries: list[Entry] = []
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                surface = (row.get("surface") or "").strip()
                if not surface:
                    continue
                entries.append(
                    Entry(
                        surface=surface,
                        respell=(row.get("respell") or "").strip(),
                        ipa=(row.get("ipa") or "").strip(),
                        notes=(row.get("notes") or "").strip(),
                    )
                )
        return cls(entries)

    def apply(self, text: str) -> str:
        for pattern, repl in self._subs:
            text = pattern.sub(repl, text)
        return text

    def surfaces(self) -> set[str]:
        out: set[str] = set()
        for e in self.entries:
            out.add(e.surface)
            out.add(e.surface.rstrip("'s"))
        return out

    @staticmethod
    def append_candidates(path: str, names: list[str]) -> int:
        """Append `surface,,,` rows for names not already present. Returns count added."""
        existing: set[str] = set()
        header = "surface,respell,ipa,notes\n"
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
            if rows:
                header = ",".join(rows[0]) + "\n"
                existing = {r[0].strip() for r in rows[1:] if r and r[0].strip()}
        new = [n for n in dict.fromkeys(names) if n not in existing]
        if not new:
            return 0
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        write_header = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            if write_header:
                fh.write(header)
            writer = csv.writer(fh)
            for name in new:
                writer.writerow([name, "", "", "candidate — verify by ear"])
        return len(new)
