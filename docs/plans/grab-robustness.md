# Plan: robust sync/grab and a thin extension

Status: **done** (2026-11-05), parts A-D. Part A (extension + protocol 2) came first because
it needs a reinstall. Deviations: the watchdog threshold is a fixed `stall_timeout`
(1800 s) rather than 3x the estimated chapter time; the `wait` text does not yet carry
per-chapter progress. Background and the code as it stands:
[SYNC-AND-FETCH.md](../SYNC-AND-FETCH.md).

## Problem

After an OS update the spaCy tagger vanished. Render raised `SystemExit`; `run_stage`
catches only `Exception` per chapter, so the chapter stayed `parsed`, never `error`.
`GrabLoop._unrendered()` counts `fetched/parsed` rows, so `/next` answered
`wait: rendering` for ever, and the idle timeout never fired because the extension kept
polling. Underlying flaws:

1. `SystemExit` means three things (missing dependency, site refusal, one bad chapter)
   and no caller can tell which.
2. `grab` paces on a DB count across *every series in scope*, not on its own backlog.
   Another series' leftover `parsed` chapter wedges it with no failure at all.
3. Liveness is "the browser asked recently", not "rendering is progressing".
4. The extension carries a state machine (run in `chrome.storage`, a staleness cut-off,
   a polling loop, two modes, its own stop rules) that the server also has.

## Policy (decided)

- A restart then a sync attempts fetch / parse / render for everything outstanding and
  carries on while the pipeline works.
- A **bad chapter** is marked `error`, the run continues past it, and the **next run
  retries it** (to recover from ephemeral errors). Once per chapter per run, so a bad
  chapter cannot loop. This holds for `sync` (already so, via `outstanding`) and for
  `grab` (new).
- An **environment failure** (tagger, TTS model, ffmpeg, espeak, lexicon) stops the run
  early with one clear message. It is never stamped on every chapter.

## Part A: thin extension, protocol 2 (ship first; needs a reinstall)

The extension's whole job: *load a URL the server names, wait for the page to settle,
send the HTML, show the server's status text.* The server owns all state and every
decision.

Changes:

- **Stateless.** The run (`port:token`) rides in the URL hash of every page the server
  sends the browser to: `#wa=<port>:<token>`. A page load with the hash is a run; without
  it, the extension does nothing. `chrome.storage` no longer holds a run; no staleness
  logic; no resume.
- **Long-poll `/next`.** `GET /next?wait=<s>` holds the request (server side, up to 25 s)
  until the answer is not `wait`. The extension stops sleeping and polling, and it has no
  `wait` state of its own.
- **Server-authored status.** Every `/next` answer carries `say`, the text to show. The
  extension no longer formats or interprets reasons.
- **No page judgement in the extension.** It waits up to 15 s for `.chapter-content`
  (so a challenge that clears itself can), then sends whatever is there. The server
  already rejects bot-check and non-chapter pages (`ingest_raw`) and ends the run with the
  reason.
- **Retired:** the manual "read the next N chapters" mode, the "save series page" panel
  and the Downloads save (`background.js` `save`, the `downloads` permission). `grab`
  does all of it through the server. The `ingest` CLI is unchanged (it still files any
  saved page). The two UI menu items that used those modes are removed; "Open in browser"
  stays.
- **Kept, small:** Stop (POST `/stop`), update button when a newer extension is
  bundled, close the tab after a clean finish (option `autoClose`), protocol check.
- `PROTOCOL` 1 -> 2 (both sides); extension 0.4.0 -> 0.5.0. `/page` is unchanged.
  Old extension against a new server (or the reverse) gets the existing 426 message.

Result: `content.js` goes from 246 to about 110 lines, and has one mode.

Tests: `tests/test_grab.py` gains long-poll (`/next?wait=` returns as soon as the render
settles, and returns `wait` on timeout) and `say` cases; `tests/ui/open_chapter.tcl`
loses the two removed items.

## Part B: stop the wedge, pace on the backlog

1. `GrabLoop._render_one`: any exception that escapes the render ends the run with
   `render failed: <reason>` (clean = false). The extension shows it. Chapter stays
   `parsed`.
2. Pacing: `if self._inflight > self.ahead` replaces the `_unrendered()` DB count;
   delete `_unrendered`. Leftovers from earlier runs are queued per series when that
   series starts, as now.
3. `grab` retries `error` chapters that already have a raw page: queue them once per run.

Tests: render that raises `SystemExit` -> `/next` returns `done` with the reason and the
thread exits; two series with B holding a `parsed` chapter -> A's first `/next` is
`fetch`; an `error` chapter is retried once per run.

## Part C: failure taxonomy and preflight

- New `src/webnovel_audio/errors.py`: `ContentError(Exception)`,
  `DependencyMissing(SystemExit)` (still a `SystemExit` so the CLI prints the message and
  exits as it does today).
- Raise `DependencyMissing` at pipeline.py (tagger, model), synth/kokoro.py, audio.py,
  espeak.py. Raise `ContentError` for a bad chapter at royalroad.py:270,
  scribblehub.py:173, local_html.py:17. Site refusals stay `SystemExit`.
- `run_stage`: catch `DependencyMissing` *before* the broad handler (it is a
  `SystemExit`): settle in-flight masters, emit an `abort` event, re-raise; never mark a
  chapter. `ContentError` is an `Exception`, so it is marked `error` and the run goes on.
- `pipeline.preflight(cfg, backend)`: load tagger, TTS, ffmpeg once. Called at the start
  of a rendering `run_stage` and in `GrabLoop.run` before the browser opens.

Tests: `tagger.load` returning `None` -> `DependencyMissing`, no status changed;
`ContentError` on chapter 1 -> chapter 1 `error`, chapter 2 renders; grab preflight
failure -> `on_ready` never called.

## Part D: progress watchdog

- `_last_progress` is bumped on `chapter_begin`/`chapter` events and on ingest.
- If work is in flight and nothing has progressed for `stall_timeout` (3x the estimated
  chapter time, minimum 20 min), finish with `render stalled`.
- `/next` `wait` answers carry `progress` (chapter, seconds in flight) in `say`.

Test: a render blocked on an `Event`, `stall_timeout=0.5` -> `done`, `clean` false.

## Leave alone

The chapter state machine, `outstanding` semantics, monotonic `mark_stage`, rendering
pacing the fetch, the token/Host/Origin checks, the `ingest` CLI.

## Risks

- Part A needs the extension reinstalled; an old extension against the new server is told
  to update (426), and a new extension against an old server is told the server is older.
- `sync` will now continue past a bad chapter where a `SystemExit` used to abort it.
- Preflight adds one spaCy load per run.
- The watchdog could trip on a cold model load: hence the 20-minute floor.
