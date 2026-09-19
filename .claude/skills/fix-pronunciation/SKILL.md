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

```
webnovel-audio series path <slug>
grep -n -i -C1 '\b<word>\b' "$(webnovel-audio series path <slug>)"/chapters/<NNN>-*.md
```

Pick one representative sentence — that's the "use case." If the word
doesn't appear (wrong chapter, or the user misremembered), say so rather
than guessing; ask which chapter, or proceed without one — the fix doesn't
require a chapter, only the word.

## 2. Show the current (wrong) pronunciation

```
webnovel-audio pron "<the sentence from step 1, or just the word>" --series <slug>
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
webnovel-audio lex add <slug> "<surface>" [--pos POS] "<respell>" [--note "<why>"]
```

- `<surface>` is the word/phrase exactly as it appears in the text
  (case-insensitive match, but keep natural casing).
- Quote multi-word phrases as one argument — phrases ignore `--pos` entirely
  and match verbatim, so use a phrase instead of a POS tag when the reading
  depends on meaning rather than grammar (e.g. `"a tear in"`).
- Keep `--note` short: why, not what.

If the mispronunciation is a generic English word rather than something
specific to this series (a heteronym like `diviner` or `inky`, not a
character name like `Broadsky`), add it straight to the base lexicon
instead with `--base`, so every series benefits:
```
webnovel-audio lex add <slug> "<surface>" [--pos POS] "<respell>" --base
```
Only do this when you're confident the fix is series-agnostic — a fix
already sitting in a series file can be moved later once that's confirmed:
```
webnovel-audio lex promote <slug> "<surface>" [--pos POS]
```
(`--pos` only needed to disambiguate if that surface has more than one rule
in the series file.)

## 6. Verify it stuck

Re-run the same sentence from step 1 through `pron --series`:
```
webnovel-audio pron "<the sentence from step 1>" --series <slug>
```
Confirm the `with lexicon` block now shows the corrected reading. Report
the file and line changed; don't commit or push unless asked.
