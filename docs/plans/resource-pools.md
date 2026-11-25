# Plan: resource pools instead of one sync lock

Status: **in progress** (2026-11-25).

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
