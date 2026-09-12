# TODO

Known gaps / follow-ups. Personal project — not a promise of when.

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
