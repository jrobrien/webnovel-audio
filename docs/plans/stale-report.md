# Plan: stale report (read-only artifact graph)

Status: **done** (2026-11-25).

## Why

Every file in a series bundle is made from known inputs, and several pieces of
freshness checking already exist, each on its own:

- `.recipe` beside each `.opus` (`pipeline.audio_recipe`): hash of the
  post-lexicon segment script, audio/dsp/pause settings, sample rate, backend,
  synth fingerprint. `render` skips a chapter whose recipe matches.
- the segment cache (per-segment audio keyed by text + voice + fingerprint).
- episode sidecars (`episodes._fresh`): member audio stat + series tags.
- `retag` rebuilds Vorbis tags from the DB, but only when run by hand.

Nothing ties these together, so the user cannot ask "what is out of date, and
why?". Recent cases: a corrected author needed a manual `retag` then a manual
episode rebuild; lexicon fixes never reach chapters already rendered; chapter
`.md` front matter still says `author: "My Profile"`.

This phase only **reports**. It changes nothing and writes nothing. Automated
rebuilds come later (see `rerender-queue.md`, whose `queue add --stale`
producer is this report's audio check).

## The graph

Per chapter (rendered chapters only, except `text`):

| node | made from | stale when | fixed by |
|---|---|---|---|
| `text` (`<stem>.md`) | raw page, provider parser, chapter/series rows | md missing; raw newer than md; front-matter fields that come from the DB (title, author, …) differ from the DB | re-parse (`parse --target S --range R`) |
| `audio` (`<stem>.opus`) | segment script (text + lexicon + cast + segmenter), `[audio]`/`[dsp]`/`[pauses]`, sample rate, backend, synth fingerprint | recomputed recipe ≠ stored `.recipe`; `unknown` when there is no `.recipe` | `render --target S --range R` (recipe-unchanged chapters are skipped by render) |
| `tags` (Vorbis comments) | series/chapter/volume rows, `[metadata]` | `--tags` only: actual stream tags (ffprobe) ≠ `retag._full_tags`; report the differing keys | `retag --scope S` |

Per series:

| node | stale when | fixed by |
|---|---|---|
| `episodes` | `chapters_per_episode > 1` and: a planned episode file is missing, `_fresh` says its members or tags changed, or an orphan file is in `episodes/` | next `render`, or `episodes --scope S` |
| `toc` (informational) | how long since the chapter list was last checked, if the DB records it | `refresh` |

The feed is generated per request, so it is never stale.

## Recipe parts (small format change)

The audio check can say *that* a chapter is stale but not *why*, because
`.recipe` holds one hash. Change `audio_recipe` to also return per-part hashes
(`segs`, `audio`, `dsp`, `pauses`, `sr`, `backend`, `fp`) and write `.recipe`
as:

```
<overall hash>
{"segs": "…", "audio": "…", …}
```

`_recipe_matches` compares the **first line only**, so existing one-line files
stay valid and nothing re-renders because of this change. A stale chapter whose
recipe has no parts line reports reason `recipe (no parts recorded)`.

When `segs` differs, diff the recomputed segments against the stored
`<stem>.segments.json` and show the first few changed segment texts
(e.g. `"the rebar" → "the reebar"`). Best-effort: `render --dry-run` also
rewrites `segments.json`, so the baseline can be newer than the audio. Say so
in the docstring; don't fix it here.

## Command

```
webnovel-audio stale [--scope S] [--range R] [--tags] [--why] [--json]
```

- No scope: every enabled series.
- Text output, per series with anything stale: counts per node and reason,
  then the command that fixes it, with a compact range:
  `audio: 14 stale (segments 12, synth model 2) → webnovel-audio render --target S --range 12,40-44`
- `--why`: per chapter lines, including the segment diff.
- `--json`: one object per series with every stale chapter, node, reasons.
- Progress on stderr for long runs.
- Exit 0 whether or not anything is stale (it's a report).

## Constraints

- **Read-only.** Open the DB without migrating or writing (the way
  `progress.py` opens it with `mode=ro`, or `DB(init=False)` plus no writes).
  Never call `pipeline.prepare` (it writes `segments.json` and markdown); use
  `pipeline.build_script` with the same `Document` the render path builds
  (`sync._load_doc`) and `sync._series_cfg`. A test snapshots the bundle's
  file mtimes and the DB bytes before and after and asserts no change.
- **No model load.** `cache.current_fingerprint` hashes files without loading
  Kokoro; compute it once per series config.
- **Reuse, don't duplicate**: `pipeline.audio_recipe`, `retag._full_tags`,
  `episodes.plan`/`_fresh`, `sync._series_cfg`, `sync._load_doc`. Lexicon and
  spaCy loading must happen once per series, not per chapter.
- Speed target: whole library (~2500 rendered chapters) under ~3 minutes
  without `--tags`. Measure on the real library (read-only, so safe) and
  record the number in the log.
- Provider isolation: no site-specific logic outside `providers/`.

## Tests

- Fresh chapter → nothing reported (null backend, tmp library).
- Lexicon row added that hits a chapter → that chapter `audio` stale with
  reason `segments`, and the diff shows the respelled text; a chapter without
  the word stays fresh.
- `[audio]` setting changed → stale with reason `audio`.
- Old one-line `.recipe` still matches (no re-render after upgrade).
- Missing `.recipe` → `unknown`.
- Series author changed in DB → `text` stale (front matter) and, with
  `--tags`, `tags` stale on `artist`.
- Episodes: member re-rendered → stale; orphan file → reported.
- Read-only snapshot test.
- `--json` shape.

## Docs

README command list; `fix-pronunciation` skill step 6: after the fix, run
`webnovel-audio stale --scope <slug>` to see which rendered chapters it
affects and the render command that updates them.

## Outcome (2026-11-25)

`webnovel-audio stale` (`src/webnovel_audio/stale.py`, tests in
`tests/test_stale.py`). Differences from the plan above:

- Script changes are reported as `text/lexicon` and/or `cast/voice` instead
  of one `segments` reason: `.recipe` parts also carry `segs_text` and
  `segs_voice` hashes (not part of the overall hash). The `--why` diff aligns
  segments on their text and groups voice changes by speaker
  (`voice Mira: af_bella → af_nova (23 segments)`).
- For one-line `.recipe` files the report adds what it can still name:
  `synth model` (the DB records the fingerprint each chapter was made with)
  and the script halves, from `segments.json`.
- Front matter: `title` and `fiction` come from the page, not the DB, so they
  are compared with a fresh parse of the raw page; comparing them with the DB
  would report differences no re-parse can fix (a series retitled since).
  `author`, `chapter`, `published`, `provider`, `source_id` are compared with
  the DB.
- `--tags` ignores `RENDERED_AT` (a render stamps its start, the DB its end)
  and maps ffmpeg's renamed keys (`TRACKNUMBER` is read back as `track`).
- `toc` is not reported: the DB does not record when a chapter list was last
  checked.
- Speed: spaCy tagging is ~85% of the cost (~0.6 s per chapter), so chapters
  are checked on forked workers (half the logical CPUs), each inheriting the
  lexicon and spaCy pipeline loaded once per series. Whole library, read-only:
  2336 chapters in 10 enabled series, 2 min 46 s on 8 workers.
- Episodes left over when `chapters_per_episode = 1` are reported as orphans
  too (`episodes.build` deletes them).
