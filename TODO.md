# TODO

Known gaps / follow-ups. Personal project — not a promise of when.

- ~~**Content bundles**~~ — **done**, `docs/plans/series-bundles.md`. The
  layout is live, all four series migrated, and `series archive|import|scan|
  reclaim|path|migrate` round-trips a series through a tarball into a clean
  machine. Next up is the cache plan below, which bundles made smaller.

- ~~**Cache maintenance subsystem**~~ — **done**,
  `docs/plans/cache-maintenance.md`. `cache status|compact|prune|clear`, the
  FLAC switch and the synth fingerprint. 9.13 GB -> 2.4 GB. `cache verify` was
  dropped (see the plan); the remaining fix worth making is an **atomic cache
  write** — `pipeline` writes segments in place, so a render killed mid-write
  could leave a partial file that later counts as a cache hit. Two lines: write
  to `.tmp` and `os.replace`. Not observed in practice (31,092 entries scanned
  clean after a render was killed mid-run), which is why it's a note and not a
  bug.

- ~~**Watch: is the segment cache ever hiding a re-render that should have
  happened?**~~ — **resolved.** Segments live under a generation directory named
  for the synth fingerprint (model bytes, voice embeddings, lang, and the
  espeak/phonemizer/kokoro-onnx versions), so a model or g2p bump starts a new
  generation instead of reusing old audio. `chapters.synth_fingerprint` and the
  `SYNTH_MODEL` Opus tag record which generation made each file, and
  `cache status` warns when a series spans more than one. `[dsp.*]`, `[pauses]`
  and `[audio]` remain outside the key, correctly — they are applied *after* the
  cache and so are re-applied on every render anyway.

- **hidden-healer: decide whether the male narrator actually works.** Set up as
  asked — narration `am_michael` (male), CJ `af_heart` (female). But the story
  is first person, so the prose *is* CJ talking, and the split lands ~87/13:
  1309 narration lines in a man's voice against 201 of CJ's own dialogue in a
  woman's. It may read as a man retelling her account, or just as wrong. One
  line to flip in the series config:

      [voices]
      narrator = "af_nova"     # or af_sarah / bf_emma — a female storyteller

  Then `render hidden-healer` to re-do it; the segment cache means only the
  narration lines re-synthesize, dialogue is reused. The file is now
  `library/hidden-healer/config.toml` (bundle layout).

- **Surface tags and content warnings in the feed and serve page.** Per-item
  show notes are done. What's left is series-level:

  - `feed.py`: `<itunes:keywords>` from `series.tags`, and a channel
    `<description>` that leads with the warning list.
  - `serve.py`: a warnings badge and tag chips on each card in `_index()`.
    Strings are author-controlled, so they must go through `_h()`.
  - per-volume feeds (`/feed/<slug>-v2.xml`) using the cached volume cover —
    volumes are modelled now, so this is mostly routing.
  - `<itunes:explicit>`: derive from warnings — `Graphic Violence`, `Gore`,
    `Sexual Content`, `Profanity` → true; `AI-Assisted Content` and
    `Sensitive Content` alone → false. Worth a per-series override in the
    overlay.
  - Don't map `<itunes:category>`: Apple's taxonomy is a fixed list and Royal
    Road's free-form tags don't fit it. Keywords carry the same information
    without inventing a bad mapping.

  **Low priority, and possibly never.** This is a single-user library whose
  contents I chose deliberately — I already know what's in the stories I'm
  tracking, so a warning banner tells me nothing I didn't know when I added
  the series. It matters only if this ever serves someone who didn't pick the
  list: a second listener on the LAN feed, or a shared/multi-user deployment.
  Until then the metadata sitting in the DB and the Opus tags is enough.
