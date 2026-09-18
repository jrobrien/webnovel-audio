# Plan: sync monitoring

Status: **phase 1 complete** (2026-09-17). `progress.py`, `webnovel-audio
progress`, 10 tests. Phases 2-3 designed, not built.

Prompted by watching an external monitor reconstruct, badly, information the
process already had.

## Why

`sync` streams events (`sync.run_stage`'s `emit`) to `--json` consumers and to
the Tk UI, but nothing persists them, and nothing aggregates a *run*. Watching a
sync from outside therefore looked impossible, and an external monitor resorted
to polling `.opus` mtimes and walking `/proc`.

That was a false conclusion, and diagnosing why set the design:

- **`state.json` is a post-run export.** `bundle.sync_bundle` runs only after
  the whole target loop (`sync.py:834`), so it is stale for a run's entire
  duration. Reading it during a sync shows nothing moving.
- **The state DB is live.** `db.py:99` sets `PRAGMA journal_mode = WAL` —
  "UI can read while sync writes" — and each chapter row commits as it
  finishes, carrying `fetched_at`, `parsed_at`, `render_started_at`,
  `render_ended_at`, `duration_s`, `narrator`, `synth_fingerprint`, `status`,
  `error_stage`. 175+ rows had exact per-stage timing the whole time.
- **There is no worker process pool.** Parallelism is a `ThreadPoolExecutor`
  in-process (`pipeline.py:237`); the child PIDs an outside watcher sees are
  `ffmpeg` and `zstd`. CPU attribution needs `getrusage`, not a process walk.

So the gap was never instrumentation. It was **discoverability and run-level
aggregation**.

## Phase 1 — expose what already exists (done)

`webnovel-audio progress [series] [--watch N] [--recent N] [--json]`.

Opens the database with `mode=ro` so the command cannot write, cannot migrate,
and cannot block the sync it watches — deliberately *not* `db.DB`, which opens
read-write and runs `_migrate()`. Probes the sync lock with a non-blocking
`flock` that is released immediately.

Reports: whether a sync holds the lock, chapters in flight (started, not
ended), recent completions with wall/audio/ratio, per-series outstanding counts
and ETA, and errors with their stage. Zero changes to the render path.

**Known limitation.** There is no `runs` table, so "this run" is inferred: the
contiguous chain of renders working back from the newest, broken by a gap over
`RUN_GAP_SECONDS` (900). Observed mis-joining two real syncs on 2026-09-17 —
Sky Pride #197 ended 23:56:29, Aura Overload #1 started 00:01:36, a 5m 07s gap,
reported as one 38-chapter run. Phase 2 makes this exact.

## Phase 2 — persist what is emitted and then lost

Two gaps:

1. **Cache hit rate.** `_advance` returns `cached_segments` / `total_segments`
   (`sync.py:739-741`); they ride the event stream and are discarded. This is
   the number that says whether a lexicon or tagger change invalidated the
   cache, and it is currently unrecoverable after the fact. Phase 1 only sees
   it indirectly, as an implausibly low wall/audio ratio (0.02-0.03 against a
   normal ~0.19).
2. **No run record.** Three tables exist; nothing says a run happened.

```sql
CREATE TABLE runs (
  id, stage, key, started_at, ended_at, rendered, errors, skipped,
  est_seconds, actual_seconds, cpu_seconds, child_cpu_seconds, peak_rss_bytes,
  synth_fingerprint, tagger, argv, pid, host);
CREATE TABLE run_chapters (
  run_id, chapter_id, number, result, stage, elapsed_seconds, audio_seconds,
  cached_segments, total_segments, error);
```

**Seam: `emit` is already the hook.** A `Recorder` becomes a second consumer,
tee'd with the JSON emitter at `cli.py`'s two call sites. `run_stage` keeps its
logic; the handler is wrapped so a recorder bug can never kill a render.

**Commit `run_chapters` rows incrementally**, as each chapter lands — that is
what keeps `progress` working mid-flight and makes the DB the monitoring
channel rather than a post-mortem.

Resource stats at run boundaries via `resource.getrusage`: `RUSAGE_SELF`
covers the synth threads exactly, `RUSAGE_CHILDREN` separates ffmpeg/zstd. No
sampling thread — it would compete with the ONNX threads for the same cores.

## Phase 3 — the report

`runs` / `runs show <id> [--json]`: per-series breakdown, slowest chapters,
cache hit rate, estimate-vs-actual drift, errors by stage, and the synth
fingerprint and tagger setting in force.

## Deliberately out of scope

- **Thermal and per-core frequency sampling.** Needs `sensors`, is
  platform-specific, and the app should not own thermal policy. Correctly an
  external concern; leave it a seam.
- **A live HTTP/socket progress endpoint.** A read-only DB query gives the same
  thing for free.
- **Per-series `sync_bundle` mid-loop.** It rewrites files during a run; point
  monitoring at the DB instead.

## Costs

One INSERT per chapter, against ~3 min of render. WAL is already on, so
concurrent readers are safe. Schema changes are additive, matching the existing
`_migrate` pattern (`db.py:105-124`).
