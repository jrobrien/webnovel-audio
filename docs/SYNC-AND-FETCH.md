# How sync and browser fetch work

How a chapter gets from "listed on the site" to "rendered audio", and how the
browser extension takes part in it. This is a description of the code as it is,
including the places where it is fragile; see [Known problems](#known-problems).

Source files (line numbers are as of this writing):

| piece | where |
|---|---|
| chapter state machine | [`db.py`](../src/webnovel_audio/db.py) `STAGES`, `outstanding` (:532), `mark_stage` (:553) |
| stage runner (`sync`, `render`, `fetch`, `parse`) | [`sync.py`](../src/webnovel_audio/sync.py) `run_stage` (:1110), `_advance` (:1077) |
| resource pools (library, browser, synth, net-*) | [`pools.py`](../src/webnovel_audio/pools.py) `hold`, `claim_chapter`, `status`; see [section 4](#4-resource-pools) |
| page ingest from a browser | [`sync.py`](../src/webnovel_audio/sync.py) `ingest_raw` (:873), `_ingest_series_page` (:837) |
| `grab` loop and its HTTP server | [`grab.py`](../src/webnovel_audio/grab.py) `GrabLoop` (:138), `next_action` (:236), `take_page` (:282), `run` (:342), `_server` (:460) |
| extension | [`content.js`](../extension/royalroad-grabber/content.js) `serverStep` (:165), [`background.js`](../extension/royalroad-grabber/background.js) |
| render refuses without the POS tagger | [`pipeline.py`](../src/webnovel_audio/pipeline.py) (:206) |
| the CLI entry points | [`cli.py`](../src/webnovel_audio/cli.py) `_cmd_grab` (:2283) |

## 1. The chapter state machine

`chapters.status` is the **only** notion of progress ([`db.py`](../src/webnovel_audio/db.py) header).

```
new -> fetched -> parsed -> rendered          (the ordered path, STAGES)
              \-> error  (+ error_stage: which stage broke)
              \-> skipped (deliberately not wanted)
```

- `db.outstanding(series, stage)` is the **declarative** work set: unlocked chapters
  not yet at `stage`, `skipped` honoured, `error` **retried**.
- `db.mark_stage` never moves a chapter backwards.
- An explicit `--range` is **imperative**: those chapters, whatever their state.

## 2. Sync (non-browser) and the stage runner

`sync` is `series refresh` + `render` over every enabled series
([`run_sync`](../src/webnovel_audio/sync.py), :1265). Everything runs through
`run_stage`.

```mermaid
flowchart TD
    START(["webnovel-audio sync / render / fetch / parse"]) --> LOCK{"library pool<br/>(shared) free?"}
    LOCK -- "busy, no-wait" --> BUSY(["exit 2: locked event, pool + holder"])
    LOCK -- "busy" --> QUEUE["emit waiting, block until acquired"]
    QUEUE --> TARGETS
    LOCK -- "yes" --> TARGETS["targets = one series, or every enabled series"]
    TARGETS --> NEXTS{"another series?"}
    NEXTS -- "no" --> DRAIN["drain pending mastering jobs"]
    DRAIN --> BUNDLE["sync_bundle per touched series"]
    BUNDLE --> DONE(["emit done: ok / errors"])
    NEXTS -- "yes" --> BO{"provider is<br/>browser_only?"}
    BO -- "no" --> REFRESH["refresh chapter list over HTTP<br/>(if sync)"]
    BO -- "yes" --> HELD["todo = only chapters the browser already<br/>brought (raw file exists, or fetched/parsed);<br/>log how many still need grab"]
    REFRESH --> TODO["todo = outstanding(stage) or explicit range"]
    TODO --> CHAP
    HELD --> CHAP{"another chapter in todo?"}
    CHAP -- "no" --> NEXTS
    CHAP -- "yes" --> CLAIM{"declarative, and another run<br/>has it claimed or done?"}
    CLAIM -- "yes" --> SKIP["emit chapter skipped"]
    SKIP --> CHAP
    CLAIM -- "no" --> ADV["_advance(): fetch (net-provider pool) -> parse -><br/>render (synth pool), up to the requested stage"]
    ADV --> OK{"result"}
    OK -- "ok" --> MARK["mark_stage(rendered); emit chapter ok"]
    OK -- "raises Exception<br/>(incl. ContentError)" --> ERR["finish_err: status = error,<br/>error_stage = stage; retried next run"]
    OK -- "raises DependencyMissing<br/>(tagger, TTS, ffmpeg)" --> ESC["settle in-flight masters, emit abort,<br/>mark nothing, stop the run"]
    MARK --> CHAP
    ERR --> CHAP
    ESC --> DONE2(["caller sees the message"])
```

Notes on the runner:

- Rendering overlaps: while one chapter is being loudness-mastered in a thread
  pool, the next is synthesizing. DB writes stay on the calling thread
  (`drain`, `settle`).
- The per-chapter handler is `except Exception`. Failures are classified in
  [`errors.py`](../src/webnovel_audio/errors.py): `ContentError` (one bad chapter) is an
  `Exception`, so the chapter is marked `error` and the run goes on; `DependencyMissing`
  (tagger, TTS model, ffmpeg, espeak, lexicon) is a `SystemExit`, caught by
  `run_stage`'s outer handler: it settles in-flight masters, emits `abort`, marks
  nothing and stops. `pipeline.preflight` checks the environment once, before the first
  render. `error` chapters are retried by the next run (`db.outstanding`).
- For a `browser_only` site (Royal Road) `sync` cannot fetch. It renders what the
  browser has already brought and tells the user to run `grab` for the rest.

## 3. Browser fetch (`grab`)

Royal Road answers plain HTTP with a bot check, so pages are read by the user's
own logged-in browser. `webnovel-audio grab` runs a loopback HTTP server
(random port, one-time token, Host/Origin/token/protocol checks) and the
extension is its client. **The server decides what the browser loads next; the
extension only reports pages and asks.**

Threads in the `grab` process: the main thread runs `GrabLoop.run`, which pulls
chapters off an in-memory queue and renders them synchronously (`_render_one`);
a `ThreadingHTTPServer` thread answers the extension.

```mermaid
sequenceDiagram
    autonumber
    participant UI as Control UI / CLI
    participant G as grab main thread<br/>(GrabLoop.run)
    participant S as grab HTTP thread<br/>(next_action / take_page)
    participant X as Extension<br/>(content.js, stateless)
    participant RR as royalroad.com
    participant DB as state.db

    UI->>G: grab --limit N
    G->>G: library pool (shared) + browser pool, build pending series list
    G->>S: start server (port, token)
    G->>X: open series URL + #wa=port:token
    Note over X: no storage: the run is the URL hash,<br/>put on every page the server sends us to
    X->>S: POST /page {html}
    S->>DB: ingest_raw -> _ingest_series_page (chapter list, reading position)
    S->>S: queue any chapter already fetched/parsed<br/>(left over from an earlier run)
    S-->>X: {ok}
    loop until the server says done
        X->>S: GET /next?wait=20
        Note over S: held open while the answer would be wait
        alt backlog not drained
            S-->>X: {action: wait, say: Waiting: rendering}
        else a new chapter is outstanding
            S-->>X: {action: fetch, url, say}
            X->>X: sleep 4-6 s (be polite)
            X->>RR: location.href = url + #wa=port:token
            RR-->>X: chapter page
            X->>S: POST /page {html}
            S->>DB: ingest_raw: mark_stage(fetched), write .raw
            S->>G: queue (series, number)
            S-->>X: {ok}
        else nothing left, or a stop
            S-->>X: {action: done, say, clean}
            X->>X: close tab if clean
        end
        G->>G: dequeue, run_stage(rendered, one chapter)<br/>(queues for the synth pool)
        G->>DB: mark_stage(rendered) or error
    end
    G->>UI: done: N ok, M error(s)
```

Rules that matter:

- **Pacing is by rendering.** The next chapter is released only when
  `_unrendered()` ([`grab.py:221`](../src/webnovel_audio/grab.py)) is at or below
  `--ahead` (default 0). `_unrendered` counts rows whose status is `fetched` or
  `parsed` in the series in scope.
- **Liveness is "the browser asked recently".** `_last_contact` is bumped on
  every `/next` and `/page`. `idle_timeout` (300 s) ends the run only if the
  browser goes *silent*. An extension that keeps asking `/next` (each held up to
  25 s) never trips it.
- **Waiting is not stalling.** A render queued for the `synth` pool (another
  job is synthesizing) pauses the `stall_timeout` clock from its `waiting`
  event to its `acquired` event.
- **Stopping**: `/stop` from the page, Stop in the UI, or a closed output pipe.
- **State lives in two places**: the DB (durable) and the grab process (the queue,
  `_queued`, `_inflight`, `_handed`, `_given`: gone when it exits). Since protocol 2
  the extension holds none: the run is the `#wa=<port>:<token>` hash on each page.

## 4. Resource pools

Jobs queue for the resources they share instead of refusing each other
([`pools.py`](../src/webnovel_audio/pools.py); the plan is
[plans/resource-pools.md](plans/resource-pools.md)). Each pool is an `flock` on
a file beside the state DB, released when its holder exits, crash or not.

| pool | kind | taken by | for |
|---|---|---|---|
| `library` (`sync.lock`) | shared / exclusive | every job shared (`render`, `sync`, `fetch`, `parse`, `grab`); `cache prune`/`clear` and `series rename` exclusive | the whole command |
| `browser` (`locks/browser.lock`) | exclusive | `grab` | the whole run |
| `synth` (`locks/synth.lock`) | exclusive | `run_stage`, stage `rendered` | one chapter's synthesis + assembly; not its mastering |
| `net-<provider>` (`locks/net-<provider>.lock`) | exclusive | `_do_fetch` | one page fetch |

**Lock order**: `library` -> `browser` -> `synth` -> `net-*`. Nothing waits for an
earlier pool while holding a later one; `pools.hold` raises `RuntimeError` if
asked to, and also refuses to wait for a pool its own thread already holds.

- **Waiting vs `--no-wait`.** By default a job that finds a pool busy emits
  `{"event": "waiting", "pool", "holder": {pid, command, what, since}}`, logs
  `waiting for synth (held by render x #3, pid N, since 14:02)`, blocks, then
  emits `{"event": "acquired", "pool", "waited_s"}`. `render`, `fetch`,
  `parse`, `sync` and `grab` take `--no-wait` for the old behaviour: exit 2 and a
  `{"event": "locked", "pool", "holder", "path"}` line. With `--no-wait` a
  render-type job also checks `synth` before starting, so it refuses at once
  rather than at its first chapter. `cache prune`/`clear` wait only at a
  terminal; from a pipe or the UI they refuse (exit 2, `cache_locked`).
  `series rename` always refuses: it is quick, and the UI waits on its answer.
- **Holder records.** A holder writes one JSON line into the lock file once it
  has the lock (the file is opened without truncating, so a waiter never erases
  it); shared holders each keep a line. `progress` lists every pool, its
  holders, and how many wait for it (from `/proc/locks`, which it reads instead
  of probing, so it cannot make a `--no-wait` job refuse).
- **Chapter claims.** Two declarative runs over the same series computed the
  same work list. Each run claims a chapter (a byte-range lock on
  `locks/chapters.lock` at the chapter's id) from before its fetch until it is
  settled, mastering included, and a declarative run skips one claimed by
  another run, or that reached the stage since its list was made (checked again
  once `synth` is held): event `chapter` with `result: "skipped"` and a
  `reason`. The claim is needed as well as the re-check: mastering runs after
  `synth` is released, so a re-check alone sees the chapter not yet rendered.
  An explicit range claims what it can but never skips.
- **The DB is not a pool.** It is SQLite in WAL mode; every `DB` write method
  commits before returning (a test holds that), so no write transaction is open
  across a fetch, a synthesis or ffmpeg.

## Known problems

Found 2026-11-05 after an OS update replaced Python (and the tagger model with it), and
fixed the same day; the plan is [plans/grab-robustness.md](plans/grab-robustness.md).

1. **A failed render wedged `grab` for ever.** The tagger-missing failure raised
   `SystemExit`; `run_stage` caught only `Exception`, so the chapter stayed `parsed`;
   `grab` counted `parsed` rows as "rendering" and told the browser to wait, and the idle
   timeout never fired because the browser kept asking. *Fixed:* a render that raises out
   of `run_stage` ends the run with the reason; `DependencyMissing` is separate from a bad
   chapter; `grab` paces on its own backlog; a watchdog gives up on a stalled render
   (`stall_timeout`, default 1800 s).
2. **A second wedge with no failure at all.** `_unrendered` counted `fetched/parsed`
   chapters across every series in scope, so series B's leftovers held series A at `wait`.
   *Fixed:* pacing is `_inflight > ahead`.
3. **One bad chapter aborted a whole `sync`** (`SystemExit` from the providers), and an
   abort stranded an in-flight master at `parsed`. *Fixed:* `ContentError`; the abort path
   drains first.
4. **`grab` never retried `error` chapters.** *Fixed:* an `error` chapter whose page is
   held is queued once per run. (An error with no page, such as a non-chapter entry, is
   left alone: fetching it again would just be rejected.)
5. **The extension carried a state machine** (run state in `chrome.storage`, a staleness
   cut-off, a polling loop, a second manual mode). *Fixed in protocol 2, extension 0.5.0.*
6. **Still true:** within one run a chapter is attempted once (`_queued`); in-process
   state (`_queued`, `_inflight`, `_handed`, `_given`) is not recorded anywhere durable.
7. **One lock for everything** (2026-11-25): `sync.sync_lock` was a single exclusive,
   non-blocking lock held for a whole command, so `render` of one chapter refused while a
   `grab` ran for an hour, and a `grab` waiting on the browser kept anything else from
   synthesizing. *Fixed:* [section 4](#4-resource-pools).
