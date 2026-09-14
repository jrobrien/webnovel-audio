# Pronunciation lexicons

CSV, columns `surface,respell,ipa,notes`:

- **surface** — the word as it appears in the text (whole-word, case-sensitive).
- **respell** — plain "sound it out" text that replaces `surface` before it
  reaches the voice. Lowercase pseudo-syllables joined by hyphens
  (`mahnt-gum-uh-ree`). **Don't uppercase for stress** — the g2p reads capital
  runs as initials (`GUM` → "gee-you-em"). Use `uh` for the weak vowel; spell a
  stressed syllable in full (`gum`, not `guh`).
- **ipa** — reserved for a future phoneme-aware backend; ignored today.
- **notes** — for you.

Lines starting with `#` are comments. (A comment *before* the header row used to
void the whole file silently — `csv.DictReader` took it as the field names — so
the loader strips them now. Quote any field containing a comma.)

## Two layers, stacked

| file | scope |
|---|---|
| `_base.csv` | **always applied**, every series and every `render`. Standard names the default g2p gets wrong (Montgomery, Eleanor, …). |
| `library/<slug>/lexicon.csv` | that series only. A row here **overrides** the same `surface` in `_base.csv`. Lives in the series bundle — `series path <slug>` finds it. |

`sync` picks up `<slug>.csv` automatically; `webnovel-audio lexicon <chapter> --write`
appends unknown proper nouns to it as blank rows for you to fill in by ear.

Change the base path with `[general] base_lexicon` in `config.toml` (set it to
`""` to disable the base layer); change the per-series directory with
`[general] lexicon_dir`.
