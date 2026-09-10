# Per-series config overlays

Drop `<series-slug>.toml` here and `sync` merges it over `config.toml` for that
series only. Same sections as `config.toml`; only the keys you set change.

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
