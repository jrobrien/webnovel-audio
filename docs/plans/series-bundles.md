# Plan: content bundles

Status: **proposed**, not implemented. Revision of the "self-contained series
directory" RFC, narrowed to the *content bundle* compromise: everything that
describes a series moves into its directory, the operational state DB stays
global and becomes rebuildable.

## Decision summary

| | |
|---|---|
| Bundle holds | manifest, config overlay, lexicon, chapters, raw, covers, cache |
| Global and shared | `state.db`, `_base.csv`, root `config.toml`, Kokoro models |
| Identity | `manifest.toml` — machine-written, separate from `config.toml` |
| Rebuild path | `series import <path>` / `series scan <root>` from `state.json` |
| NFS / removable | **out of scope** — bundles stay on local storage |

Dropped from the full-bundle version: per-series SQLite. That was the source of
the WAL/tar hazard and most of the migration cost, for the component with the
least to gain (229 KB across all four series).

## Layout

```
<bundle-root>/sky-pride/
  manifest.toml              identity + provenance     machine-written
  state.json                 exported chapter state    machine-written
  config.toml                the overlay               hand-edited
  lexicon.csv                                          hand-edited
  covers/
    cover.jpg  cover-v10395.jpg  …
  chapters/
    001-chapter-1-….md
    001-chapter-1-….opus
    001-chapter-1-….segments.json
  .raw/
    <rr_id>.html
  .cache/
    <backend>/<fingerprint>/<sha1>.flac
```

`chapters/` is new — today 282 files sit flat in `library/sky-pride/`, which is
unpleasant to poke around in. Covers move to `covers/` for the same reason.

### Three files, three authorities

The split exists because of *who writes them*, not tidiness:

- **`config.toml`** is hand-edited — by the user, and in place by `cast set`,
  which preserves trailing comments. Machine-churning fields must not live in a
  file a comment-preserving rewriter is editing.
- **`manifest.toml`** is machine-written and machine-read. `series scan` can
  read identity from it without parsing config layering, and a bad hand-edit to
  `config.toml` never makes a bundle unidentifiable.
- **`lexicon.csv`** unchanged in format; just relocated.

```toml
# manifest.toml — written by webnovel-audio; hand edits will be overwritten
schema        = 1
uuid          = "5f8c1e6a-…"            # stable identity across moves/import
slug          = "sky-pride"

[source]
provider      = "royalroad"
id            = 105229
url           = "https://www.royalroad.com/fiction/105229/sky-pride"
title         = "Sky Pride"
author        = "…"
added_at      = "2026-09-08T20:41:03"

[provenance]
last_synced_at      = "2026-09-13T11:04:22"
base_lexicon_sha256 = "a3f1c8d2…"       # warn, don't copy — see Open tensions
base_config_sha256  = "9c2b4e07…"
synth_fingerprints  = ["a3f1c8d29e04"]  # >1 means a mixed-generation series
```

`[source]` answers "keeping the provider type and urls inside the series tree" —
the bundle names its own origin, so `series import` works with no network and no
prior knowledge.

## The global DB, and how it gets rebuilt

`state.db` stays at `~/.local/state/webnovel-audio/state.db` with its current
schema, plus two columns on `series`:

```sql
ALTER TABLE series ADD COLUMN uuid TEXT;   -- matches manifest.toml
ALTER TABLE series ADD COLUMN path TEXT;   -- absolute bundle location
```

`path` is absolute and machine-local, which is correct: the DB *is* machine-local.
Every path stored per chapter becomes **bundle-relative**
(`chapters/001-….opus`), resolved as `series.path / rel`. No absolute path may
enter a chapter row.

### `state.json` — the export that makes import exact

The DB is the operational store; `state.json` is a plain export of this series'
rows, rewritten atomically at the end of each sync. It is:

- **readable** — the user pokes around, and this is the file that says what
  state each chapter is in
- **diffable** — shows up sensibly if a bundle is ever version-controlled
- **the import source** — on a fresh machine the DB has nothing, so this becomes
  authoritative

Without it, import would have to infer state from file presence (`.raw/*.html`
-> fetched, `.md` -> parsed, `.opus` -> rendered) plus Opus tags. That recovers
most fields but **loses `render_started_at` / `render_ended_at`** — the timing
data behind `estimate_render` and the render-cost model — and can't recover
`published_at`, `volume_rr_id`, or `unlocked` at all without a network round
trip.

Authority rule, to be documented and tested: **while a bundle is tracked, the DB
wins; `state.json` is authoritative only at import.** A sync that crashes
mid-write leaves a stale export, not a split brain.

Complementary and cheap: add `RENDER_SECONDS` to the Opus tags alongside the
already-planned `SYNTH_MODEL` and existing `RENDERED_AT`. Then even a bundle
whose `state.json` is lost can reconstruct timings from the audio files
themselves.

## Commands

```
series import <path>          adopt a bundle: read manifest, replay state.json
series scan  <root>           bulk import / re-locate everything under a root
series path  <key>            print the bundle path (agent- and cd-friendly)
series archive <key> --out F  clean tar; --with-cache to include .cache
```

`series refresh` already re-fetches chapter lists and remains the network-side
repair. `import` is the offline one.

**Relocation is `mv` plus `series scan`.** The index must treat a missing `path`
as *stale*, never as deletion: look for the uuid under known bundle roots before
complaining. Deletion stays explicit via `series forget`, so an unmounted drive
never silently drops a series.

**Archive excludes `.cache/` by default.** Measured: sky-pride is 575 MB of
output against 5.86 GB of cache. Including regenerable data would make every
archive 10x larger.

## How this interacts with the cache plan

`docs/plans/cache-maintenance.md` should be read as amended by this, and the
interaction is strongly favourable:

- **`cache clear --series` becomes `rm -rf <bundle>/.cache`.** The entire
  "exclusively-owned keys" analysis disappears.
- **`live_keys` shrinks to one bundle's `segments.json` files.** The catastrophic
  failure mode — a missing `library/` tree making 26k live keys look orphaned —
  can no longer span series. The manifest guard is still worth having but
  defends a far smaller blast radius.
- **`series forget --purge` is fixed by construction**, rather than by
  remembering to call `cache clear` first. That leak was the original symptom of
  the scatter this plan removes.
- Losing cross-series dedup costs **28 keys, 0.01 GB** (measured, 0.1%).
  Different books don't share sentences.

**Sequencing consequence: decide the bundle layout before running `cache
compact`.** Compact is one full pass over 28,195 files; it may as well write
them to their final per-bundle home. Doing it twice is the only avoidable waste
in either plan.

## Migration

One command, `migrate bundles`, with a DB backup first (precedent:
`state.db.pre-0.2.bak`). Per series:

1. `mkdir chapters/ covers/`, move `NNN-*.{md,opus,segments.json}` and
   `cover*.jpg` into them; `.raw/` is already in place.
2. Move `data/series/<slug>.toml` -> `<bundle>/config.toml`, and
   `data/lexicons/<slug>.csv` -> `<bundle>/lexicon.csv`.
3. Write `manifest.toml` (uuid generated now) and `state.json`.
4. Rewrite `audio_path` / `raw_path` / `text_path` to bundle-relative; set
   `series.uuid` and `series.path`.
5. Move this series' cache entries into `<bundle>/.cache/` — folded into
   `cache compact` so files are touched once.

Scale: 4 series, 559 chapters, 820 MB output, 9.13 GB cache.

### Call sites

19 references to `library_dir` / `state_db` outside `config.py`. The concentrated
work is:

- `sync.py` — `_dir_slug`, `_cache_cover`, raw/text path builders (lines ~93-183,
  508-566, 729)
- `serve.py` — `library()` at :126 and the `/audio/<slug>/…` mapping at :164,
  :170, :193, :243 must resolve through `series.path` instead of assuming one
  root
- `cli.py` — `forget --purge` at :898, `library_dir` in the info block at :1034

This path rewrite is the largest edit in the proposal and is **all-or-nothing** —
a half-migrated scheme is worse than either end state.

### What does not break

Feed GUIDs are `rr-<rr_id>` with `isPermaLink="false"` (`feed.py:143`), so
**existing podcast subscriptions survive the move.** Enclosure URLs change;
AntennaPod keys on GUID and will not re-download.

## Open tensions

### Base lexicon and config disconnect

Unchanged from the RFC, and still the sharpest problem: a global `_base.csv`
exists so that fixing `Montgomery` fixes it everywhere, which a per-bundle copy
would defeat.

Resolution: **don't copy it in.** Record `base_lexicon_sha256` and
`base_config_sha256` in the manifest and warn on mismatch at render time:

```
warning: rendered against base lexicon a3f1c8d2, current is 9c2b4e07
         14 entries differ — chapters 1-53 used the old set
```

This is safe because already-rendered audio needs no lexicon at all — the
`.opus` files are finished artifacts, and an un-tarred bundle is fully
listenable on a bare machine. The disconnect only affects *new* renders, where a
warning is the right response.

Note it composes with the fingerprint work rather than duplicating it: lexicon
substitution happens *before* the cache key is computed, so a lexicon change
already invalidates the cache correctly. The manifest hash is for **provenance**
— the same "is this book internally consistent?" question that
`chapters.synth_fingerprint` answers for the model, reported through the same
surface.

### Which config sections pin at init

Pinning default voices into the series overlay at init already made this
decision once; the bundle makes it worth stating as a rule.

- **Pinned at init** (anything that changes the sound, so a bundle is
  sound-complete by construction): `[voices]`, `[cast]`, `[synth]`, `[chat]`,
  `[dsp]`, `[pauses]`, `[audio]`
- **Always live** (anything about *this machine*): `[general]` paths,
  `[royalroad]` network/auth/state_db, `[serve]`

A bundle with no base config available therefore renders correctly; only
machine-local settings fall back to defaults.

### Honest scope note

For four series this is over-engineering measured against today's pain. What
justifies it is that three things already wanted all reduce to "the directory is
the unit": archival to slow storage, correct `forget --purge`, and per-series
cache scoping — and the cache plan's most dangerous machinery exists only
because the cache is global.

## Sequencing

1. **`manifest.toml` + `state.json` writers**, emitted into the existing
   `library/<slug>/` layout. No moves yet, nothing breaks, and it makes the
   export format reviewable on real data.
2. **`series import` / `scan` / `path`**, validated by round-tripping a bundle
   into a scratch DB and diffing against the live one. This proves
   rebuildability *before* anything depends on it.
3. **`migrate bundles`** — the file moves and the path rewrite, folded into
   `cache compact` so the 28k-file pass happens once.
4. **`serve.py` / `sync.py` path resolution** through `series.path`.
5. **`series archive`**, and `forget --purge` simplified to a directory removal.

Steps 1-2 are additive and reversible; step 3 is the commitment.
