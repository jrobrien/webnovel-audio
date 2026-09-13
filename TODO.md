# TODO

Known gaps / follow-ups. Personal project — not a promise of when.

- **Watch: is the segment cache ever hiding a re-render that should have
  happened?** Not a known bug — investigated once (sky-pride #57 finished in
  16 s from a right-click Render in the UI) and the cache hit was legitimate:
  all 194 segments had been synthesized nine minutes earlier, the output was
  full-length real audio, and the fast path is only loudnorm + encode.

  Why it *should* be safe: the cache key is
  `text|voice|style|rate|pitch` + sample rate (`pipeline._cache_path`), and
  text is post-normalize/post-lexicon. So any change to the lexicon, the cast
  voices, `[synth] speed`, or the prose itself changes the key and misses.
  The `[n/m segments cached]` line now makes the reuse visible.

  What would *not* miss, and is the thing to suspect if output ever looks
  stale: a change that alters audio **without** touching any of those five
  fields — `[dsp.*]` chains, `[pauses]`, `[audio]` loudness, or a Kokoro
  version bump. Those are applied after the cache, so DSP/pauses/loudness do
  get re-applied on every render and are fine; a **model change is the real
  hole**, since cached wavs from the old model would be reused silently.

  If it comes up: `rm -rf .cache/segments` forces a true cold render, and
  compare. A proper fix would fold the backend identity (model file hash or
  version) into the cache key, and/or add `render --no-cache`.

- **hidden-healer: decide whether the male narrator actually works.** Set up as
  asked — narration `am_michael` (male), CJ `af_heart` (female). But the story
  is first person, so the prose *is* CJ talking, and the split lands ~87/13:
  1309 narration lines in a man's voice against 201 of CJ's own dialogue in a
  woman's. It may read as a man retelling her account, or just as wrong. One
  line to flip in `data/series/hidden-healer.toml`:

      [voices]
      narrator = "af_nova"     # or af_sarah / bf_emma — a female storyteller

  Then `render hidden-healer` to re-do it; the segment cache means only the
  narration lines re-synthesize, dialogue is reused.

- **Surface source metadata in the feed and the serve page.** Tags, content
  warnings, status and rating are already captured (`series.tags` /
  `.warnings` / `.status` / `.rating`, populated by `parse_fiction` on every
  refresh) and already ride along in the Opus tags as `KEYWORDS` /
  `CONTENT_WARNING`. What's missing is the delivery end:

  - `feed.py`: `<itunes:keywords>` from tags, `<itunes:author>`, and a
    `<description>` / `<itunes:summary>` that leads with the warning list.
    Per-item show notes (source URL, publish / fetch / render times, narrator
    voice) rather than the current bare enclosure.
  - `serve.py`: a warnings badge and tag chips on each card in `_index()`.
    Strings are author-controlled, so they must go through `_h()`.
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
