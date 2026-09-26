---
name: fix-pronunciation
description: >
  Pin a mispronounced word or phrase to the correct reading in this project's
  pronunciation lexicon. Use when the user reports that the TTS says a word
  wrong — a character/place name, a heteronym read with the wrong grammar, or
  any word that just comes out sounding off — optionally naming a chapter or
  series. Triggers: "X should be pronounced Y", "X sounds wrong", "in chapter
  N the word X is mispronounced", "fix the pronunciation of X". Covers finding
  the word in context, previewing the new reading before touching any file,
  writing the fix with `webnovel-audio lex add`, and promoting a fix that
  turns out to be series-agnostic to the base lexicon with `lex promote`.
---

# Fixing a pronunciation

This is a CLI-only task — every step below is one `webnovel-audio` (or
`grep`) command. No source reading required. Run commands with `uv run
webnovel-audio ...` from the project root.

## 0. Read the request

You need: the **series** (slug or a name to match against `series list`),
the **word or phrase**, and the **desired reading** (either a respelling the
user already gave you, like "dee-vine-er", or a description like "should
rhyme with wine, not win"). A **chapter number** is optional context, not a
requirement — most fixes come from the user just naming the word.

If the series isn't obvious, list them:
```
webnovel-audio series list
```

## 1. Find the word in context (only if a chapter was given)

Get the series' bundle directory, then grep its chapter file. Chapter files
are named `NNN-slug-title.md` inside `chapters/`, zero-padded to 3 digits.

**`NNN` is the only number any command accepts, and it is usually not the
number in the chapter's title.** A title reading "Chapter 23" can be file 355:
the title text is the author's own numbering, per volume, and nothing indexes
it. If the user names a chapter by title, or by a number that finds nothing,
search the titles instead:

```
webnovel-audio state show --scope <slug> | grep -i '<words from the title>'
```

That prints `#NNN status title`, which gives you the file number to grep for.
Long titles are truncated in the text output; add `--json` when the full title
matters.

```
webnovel-audio series path --scope <slug>
grep -n -i -C1 '\b<word>\b' \
  "$(webnovel-audio series path --scope <slug>)"/chapters/<NNN>-*.md
```

Pick one representative sentence — that's the "use case." If the word
doesn't appear (wrong chapter, or the user misremembered), say so rather
than guessing; ask which chapter, or proceed without one — the fix doesn't
require a chapter, only the word.

## 2. Show the current (wrong) pronunciation

```
webnovel-audio pron "<the sentence from step 1, or just the word>" --scope <slug>
```

The `≈ say` line is the reading you're correcting. This is also how you
confirm a word actually IS mispronounced before spending a lexicon row on
it — if the gloss already matches what the user wants, stop here and say so
instead of adding a no-op rule.

## 3. Decide the part of speech (skip for names/places)

- A **name, place, or loanword** that's always said the same way (a person's
  name, a fictional term, "Broadsky") → no POS. It applies to every
  occurrence.
- A **heteronym** — same spelling, different sound depending on grammar
  (`live` VERB vs ADJ, `read` past vs present) → scope it: `--pos NOUN`,
  `--pos VERB`, `--pos ADJ`, `--pos ADV`, or a Penn tag like `VBD` for a
  specific inflection. Read the sentence from step 1 to tell which sense is
  in play.
- If you truly can't tell whether it's grammar-dependent, ask the user
  rather than guessing — a wrong POS scope silently leaves other uses of the
  word unfixed.

## 4. Preview the new reading — BEFORE touching the lexicon

Feed the candidate respelling straight to `pron` as plain text. This is
exactly how the base and series lexicons work: a respell is just English
text fed back through the same g2p, so previewing it needs no lexicon edit
at all.

```
webnovel-audio pron "<candidate respell>"
```

Compare the `≈ say` output to what the user asked for. If it doesn't match,
adjust the respelling (plain English syllables, hyphens between them, e.g.
`kay-lith`, `dee-vine-er`) and preview again. Don't write anything until this
step sounds right.

## 5. Apply the fix

```
webnovel-audio lex add --scope <slug> --surface "<word>" --respell "<respell>" \
    [--pos POS] [--note "<why>"]
```

- `--surface` is the word/phrase exactly as it appears in the text
  (case-insensitive match, but keep natural casing). It may not be empty.
- `--respell` may be empty (`--respell ""`), which means "I listened, it reads
  fine" and stops `check` suggesting the word again. `lex ignore` is the
  shorthand for that.
- Quote a multi-word phrase as one argument — phrases ignore `--pos` entirely
  and match verbatim, so use a phrase instead of a POS tag when the reading
  depends on meaning rather than grammar (e.g. `--surface "a tear in"`).
- Keep `--note` short: why, not what.
- Every field is a named flag, so order never matters. An unknown `--scope`
  exits non-zero rather than quietly writing somewhere unexpected.

If the mispronunciation is a generic English word rather than something
specific to this series (a heteronym like `diviner` or `inky`, not a
character name like `Broadsky`), write it to the base lexicon with
`--scope @base`, so every series benefits:
```
webnovel-audio lex add --scope @base --surface "<word>" --respell "<respell>" [--pos POS]
```
Only do this when you're confident the fix is series-agnostic — a fix
already sitting in a series file can be moved later once that's confirmed:
```
webnovel-audio lex promote --scope <slug> --surface "<word>" [--pos POS]
```
(`--pos` only needed to disambiguate if that surface has more than one rule
in the series file.)

## 6. Verify it stuck

Re-run the same sentence from step 1 through `pron --scope`:
```
webnovel-audio pron "<the sentence from step 1>" --scope <slug>
```
Confirm the `with lexicon` block now shows the corrected reading. Report
the file and line changed; don't commit or push unless asked.
