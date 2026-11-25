# Plan: resource pools instead of one sync lock

Status: **done** (2026-11-25). Described as built in
[SYNC-AND-FETCH.md section 4](../SYNC-AND-FETCH.md#4-resource-pools); where the
build differs from this plan, see [As built](#as-built) at the end.

## Why

`sync.sync_lock` is one exclusive, non-blocking `flock` held for a whole
command by `render`, `sync`, `grab`, `cache clear/prune` and
`series rename --move`. It stops two jobs fighting over the CPU, but:

- it refuses instead of queueing: `render --range` for one chapter fails with
  "another sync is already running" (exit 2) while a `grab` runs for an hour;
- it is coarser than the resources: a `grab` holds it while it waits on the
  browser, so nothing else can synthesize during that time, and a `sync`
  fetching over the network blocks a render of another series.

The real resources are: the synthesizer (Kokoro uses every core), the browser
(one extension, one tab), each site's politeness budget, and the library
layout itself (rename/move and cache deletion must not race any job).

The state DB is **not** one of them: it is SQLite in WAL mode (many readers,
one writer at a time, `busy_timeout` 10 s), so it only needs write
transactions to stay short.

## Design

New module `src/webnovel_audio/pools.py`. Lock files live in the state
directory next to the DB: `locks/<name>.lock`, plus the existing `sync.lock`
reused as the library lock (so `progress`' probe keeps working).

```python
with pools.hold(cfg, "synth", what="render stray-cat #196",
                wait=True, emit=emit, log=log):
    ...
```

- `flock` on an open file: released when the process exits, crash or not.
- Try non-blocking first. If busy: read the holder record from the file,
  emit `{"event": "waiting", "pool": name, "holder": {...}}`, log
  `waiting for synth (held by grab, pid N, since HH:MM)`, then block.
  Emit `{"event": "acquired", "pool": name, "waited_s": x}` after a wait.
- After acquiring, write a holder record (one JSON line: pid, command,
  `what`, since) into the file. Open with `"a+"` and truncate only *after*
  acquiring, so a waiter opening the file never erases the holder's record.
- `wait=False` raises `PoolBusy(name, holder)`; `SyncLocked` becomes a
  subclass or alias so existing `except sync.SyncLocked` keeps working.
- Ctrl-C while blocked must work (Python retries `flock` on EINTR but a
  KeyboardInterrupt from the handler propagates; test it).

### Pools and who takes them

| pool | kind | held by | span |
|---|---|---|---|
| `library` (`sync.lock`) | shared / exclusive (`LOCK_SH` / `LOCK_EX`) | every job **shared**; `series rename --move`, `cache clear`, `cache prune` **exclusive** | whole command |
| `synth` | exclusive | `run_stage` for stage `rendered` | **per chapter**, around `_prepare_render` (synthesis + assembly); not around mastering, which runs on the in-process pool |
| `browser` | exclusive | `grab` | whole grab run |
| `net-<provider>` | exclusive | `_do_fetch` | per page fetch |

Depth > 1 is not needed now; if it is later, use N slot files
(`synth.0.lock`, `synth.1.lock`) and take the first free one.

Lock order, to rule out deadlock: `library` → `browser` → `synth` →
`net-*`. Never take an earlier pool while holding a later one.

### Consequences to handle

- **Duplicate work.** Two declarative runs over the same series compute
  `outstanding` up front. After acquiring `synth` for a chapter in
  declarative mode, re-read its row and skip it (event `result: "skipped"`,
  reason "done by another run") if it already reached the stage. Explicit
  ranges are left alone: the recipe check makes a duplicate cheap.
- **grab's stall timer.** `GrabLoop.stall_timeout` gives up when nothing has
  begun or finished for 30 min. Time spent waiting for `synth` must not count:
  treat the `waiting`/`acquired` events as progress or pause the clock.
- **Default is wait.** `render`, `sync`, `fetch`, `grab` gain `--no-wait`,
  which restores today's behaviour: exit 2 and the `locked` event, now
  carrying `pool` and `holder`. Update the exit-code text in the CLI's
  conventions output.
- **cache commands** keep refusing rather than waiting when non-interactive
  (they already refuse without `--yes`); interactive use may wait with the
  message.

### UI

`ui/control.tcl` `handle_event`: a `waiting` branch logs
`waiting for <pool> (held by <what>)` and shows it in the status line; an
`acquired` branch clears it. Keep the `locked` branch for `--no-wait`.
UI tests run on a private X display (`xctl`), never on the live desktop.

### progress

`progress` reports every pool: free, or held by whom since when, and who is
waiting if that can be read cheaply (it can't from `flock`; holder only is
fine).

### DB transactions

Audit `db.py` and the stage code: no write transaction may stay open across
network, synthesis or ffmpeg. Add an invariant test: after each public `DB`
write method, `db.con.in_transaction` is False.

## Tests

- `pools.hold`: second holder in another process waits, then gets it; the
  holder record is readable while held and survives a waiter opening the
  file; `wait=False` raises with the holder; released on process kill.
- `library` shared by two jobs at once; exclusive waits for both.
- Two processes rendering different series with the null backend both finish,
  and their `synth` spans never overlap (from the events' timestamps).
- Two declarative runs over the same series: each chapter rendered once.
- `--no-wait` gives exit 2 and a `locked` event naming the pool.
- grab with a held `synth`: no stall while waiting (fake render, short
  timeouts).
- DB `in_transaction` invariant.

Tests use tmp state directories only. **Do not run renders, syncs or grabs
against the real library**: the user's UI and `serve` are live on it.

## Docs

README / SYNC-AND-FETCH: the pools, waiting vs `--no-wait`, and lock order.

## As built

Where the implementation went beyond or differs from the above:

- **Chapter claims.** Re-reading the row after acquiring `synth` was not enough:
  mastering runs after `synth` is released, so the second run saw the chapter
  still `parsed` and rendered it again (measured: every chapter twice). Each
  run now also claims a chapter (`pools.claim_chapter`, a byte-range lock at
  the chapter id on `locks/chapters.lock`, one descriptor per process) from
  before its fetch until it is settled; a declarative run skips a claimed
  chapter. Explicit ranges claim what they can but never skip, so a `grab`
  render (explicit, one chapter) is still skipped by a concurrent `sync`.
  Claims are never waited on, so they sit outside the lock order.
- **`--no-wait` checks `synth` up front** for `render`, `sync` and `grab`, so the
  refusal comes at once rather than after fetching/parsing the first chapter.
  If `synth` is taken later in the run, the run stops with exit 2 after
  settling what is already mastering (`grab` keeps it in `GrabLoop.busy`).
- **`parse` and `fetch`** take `library` shared too (they write the library), so
  they also get `--no-wait`; before, `fetch`/`parse` took no lock at all.
- **`series rename` never waits**: it refuses (exit 2) as before. It is quick,
  and the UI calls it synchronously, so queueing behind an hour-long render
  would freeze the UI.
- **Shared holder records.** Several `library` holders each keep a JSON line;
  rewrites are serialised by a tiny leaf lock (`locks/records.mutex`), and dead
  pids are dropped.
- **`progress` reads `/proc/locks`** (matched by inode: on btrfs `stat`'s device
  is not the one `/proc/locks` shows) for held and waiting counts, instead of
  taking the lock to probe it, which could make a `--no-wait` job starting that
  instant refuse. Falls back to the probe where there is no `/proc/locks`.
- **Guards.** `pools.hold` raises `RuntimeError` on an out-of-order acquire and
  on waiting for a pool the same thread already holds (a self-deadlock).
- Not taken: the `browser` pool for `series add` through the browser
  (`PageCatcher`), and `net-*` around `series refresh` and cover fetches; the
  plan's table names only `grab` and `_do_fetch`.
