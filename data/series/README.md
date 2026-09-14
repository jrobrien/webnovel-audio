# Per-series config overlays

> **Moved.** These now live in the series bundle as
> `library/<slug>/config.toml`, not here — see "Where a series lives" in the
> README. `webnovel-audio series path <slug>` prints the directory, and
> `series migrate` moves a pre-bundle tree into the layout. Everything below
> still describes the file's *contents*, which are unchanged.

`sync` merges this file over `config.toml` for that series only. Same sections
as `config.toml`; only the keys you set change.

`webnovel-audio series add` writes a starter file, pinning the *resolved*
voices so the series keeps sounding the same if you retune the global defaults
later. `webnovel-audio cast update <slug> <range>` then fills in
`[cast.voices]`, one line per detected speaker, commented with its line count
and a gender guess:

```toml
[cast.voices]
"Mara"  = "af_heart"     # 8 line(s), female
"Resk"  = "am_michael"   # 6 line(s), male
```

`cast update` only ever **adds** speakers it hasn't seen, so re-running it on a
later range (once new characters appear) never rewrites what you've tuned. A
speaker whose gender it can't infer is written unassigned (`= ""`) — visible to
fix, and falling through to `[cast] default` until you do. `--diff` previews.

- `[cast.voices]` and `[chat.voices]` in an overlay **replace** that table
  wholesale (a series' cast is fully defined here).
- `[dsp.*]` **merges** per key (base chains are kept).
- everything else is a plain key override.

The per-series pronunciation lexicon (`../lexicons/<slug>.csv`) still auto-loads;
an overlay's `[general] lexicon` wins over it if set.

Example — `salvage-run.toml`:

```toml
[cast]
protagonist = "Mara"

[cast.voices]
Mara = "af_heart"
Resk = "am_michael"

[chat]
speak_location = true      # this series' chat locations matter to the plot

[dsp.Resk]
semitones = -1.0
```

Change the directory with `[general] series_config_dir` in `config.toml`.
