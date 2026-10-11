# Plan: re-render work queue

Status: **parked option** (2026-10-11). Nothing is built; revisit when wanted.

## Problem

A pronunciation fix (or an audio-setting change such as the 32k / -16 LUFS default)
leaves chapters that are still correctly `rendered` but out of date. There is no way to
say "redo these later". Demoting `status` is wrong: the player and the podcast feed list
only `status = rendered` chapters, so the chapter would vanish until the render finished,
and `status` means "furthest stage reached" (see `DB.mark_stage`). A flag on the chapter
row was rejected for the same reason: out-of-band work is not chapter state. It gets its
own table.

## Design

**Table `work_queue`** in the state DB. Machine-local, so not exported to `state.json`.

| column | meaning |
|---|---|
| `id`, `series_id`, `chapter_id` | the target; foreign keys cascade, so `series forget` cleans up |
| `kind` | `render` now; later possibly `remaster` (new bitrate/loudness, no synthesis) or `refetch` |
| `reason` | free text, e.g. "rabid pronunciation" |
| `requested_at`, `requested_by` | when, and from cli / ui / skill |
| `state` | `queued`, `running`, `done`, `cancelled`, `deferred` |
| `finished_at`, `outcome` | `rendered`, `unchanged`, `error: …`, `skipped: heard` |

One live item per chapter and kind: queuing again updates the reason. Finished items stay
as history ("what did the rabid fix touch?") and are pruned later.

**Producers**

- `queue add --scope S --range R [--note TEXT] [--force]`
- `queue add --stale --scope S [--range R]`: enqueue rendered chapters whose audio recipe
  (`pipeline.audio_recipe`) no longer matches the stored `.recipe`. The recipe hashes the
  post-lexicon segment script plus the audio settings, so a lexicon fix changes it. Costs
  parse + segment only (~50 ms/chapter), no synthesis.
- optionally `lex add --queue`, which runs the stale scan after the fix.

**Consumer**

- Drained inside `sync` (default) or by `queue run`, under `sync_lock`. At start, any
  `running` rows are leftovers from a crash (nobody else can hold the lock) and go back to
  `queued`.
- Each item goes through the normal prepare/master path with skip-unchanged on, so an item
  whose recipe already matches finishes as `unchanged` for free. `--force` bypasses that.
- Order: new chapters first, then the queue; series priority still orders series.

**Listening policy**, decided at drain time because positions change after queuing:
`sync --queue unheard|all|none`, default `unheard`.

- `unheard`: an item whose chapter is at or before any profile's furthest position
  (`positions.furthest_chapter`) becomes `deferred`, with the reason shown.
- `all` (`queue run --policy all`): drains deferred items too.
- Podcast-app listening leaves no position, so it counts as unheard; `all` and an explicit
  `render --range` are the override.

**Surfaces**

- `queue list [--scope S] [--all]`, `queue cancel --scope S [--range R]`.
- Counts ("N queued, M deferred") in `series list`, `progress` and the sync estimate.
- UI: chapter-list marker derived by a join (never stored on the chapter), right-click
  **Re-render later**, and an unheard/all/none choice with counts in the Sync dialog. Queue
  work emits the same events as render work, so live updates need no new event shape.
- `fix-pronunciation` skill: run `queue add --stale` instead of grepping for the word.

## Order of work

1. Table, `queue add` / `list` / `cancel`, the drain in `sync` and `queue run`, crash
   recovery.
2. Listening policy (`deferred`, `--policy`) and the displays.
3. `--stale`, the UI menu and dialog, the skill update.

Each phase gets its own tests (queue lifecycle, crash recovery, policy against fake
positions, stale detection after a lexicon change, the UI probe).

## Open decisions

- Drain in `sync` by default? Recommended: yes, with `unheard`.
- "Heard" = any profile's furthest position? Recommended: yes; a per-profile variant later.
- Include the `remaster` kind in phase 1? Recommended: no; a cached re-render is already
  cheap (about 3 s a chapter).
- Check that `replace_chapters` keeps chapter ids stable on a refresh, since the queue
  references them.
