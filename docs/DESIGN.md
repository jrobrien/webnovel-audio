# webnovel-audio — design

Goal: generate good audiobook-quality narration of web novels (Royal Road, etc.)
for personal listening, as an overnight batch job on a CUDA-less AMD APU laptop.

## Target hardware

| | |
|---|---|
| CPU | AMD Ryzen 7 8745HS — 8c/16t Zen 4, AVX-512 + VNNI + BF16 |
| GPU | Radeon 780M (RDNA3, gfx1103), shares system RAM, **no CUDA** |
| RAM | 32 GB (shared with iGPU) |
| Other | ffmpeg 9, `uv`, big NVMe |

Consequence: **CPU + ONNX Runtime is the reliable synthesis path.** ROCm on
gfx1103 is fragile; the one dependable GPU path is Vulkan, and only really for a
llama.cpp annotation model. Not realtime, and that's fine.

## Engines

| Role | Engine | Notes |
|---|---|---|
| Baseline narration | **Kokoro-82M** (`kokoro-onnx`) | Apache-2.0, natural prosody, faster than realtime on Zen 4. Phase 1 default. |
| Premium narration (opt) | XTTS-v2 / StyleTTS2 | Voice cloning, more expressive. ~0.1–0.3× realtime on CPU → overnight only. |
| Bulk / fallback | Piper | 20–100× realtime; flat prosody. For clearing a big backlog. |
| Speaker attribution | BookNLP | Quote → speaker, coref. CPU-fine. |
| Tone / emphasis / IPA guesses | llama.cpp (Vulkan) + Qwen2.5-7B | Annotate a chapter in a couple of minutes. |

## Pipeline

```
fetch (RoyalRoad) → clean/structure → normalize (LitRPG-aware) → annotate
(BookNLP speakers + LLM tone + thought detection) → apply per-series lexicon →
segment script (JSON) → synthesize (cache per segment) → per-voice DSP +
2-pass loudnorm → assemble → Opus / chapter-marked M4B → deliver (local podcast
RSS feed the phone subscribes to)
```

## The text pre-pass (where quality comes from)

- **Normalize**: curly quotes, ellipses, em-dashes, zero-width chars, thousands
  separators, `Lvl`→level, integers/ordinals→words. Strip hidden anti-piracy nodes.
  Fold shouted ALL-CAPS to normal case (`_dampen_caps`) — the g2p spells a short
  all-caps token letter by letter ("DAMN IT!" → "damn eye-tee"); a shouting run or
  a ≤2-letter token is downcased, lone acronyms ("the FBI", "Chapter IV") survive.
  Drop non-Latin letters (Han/Kana/Hangul/Cyrillic/Arabic — the g2p narrates them
  as "chinese letter …" or spells them out) and strip inverted `¿`/`¡`. Accented
  Latin is kept; the g2p is pinned to `en-us` and language auto-switch is never
  enabled, so a Spanish-looking word gets English letter-to-sound, not an accent.
- **Internal monologue**: whole-paragraph/sentence italics not inside quotes →
  a distinct, consistent `thought` voice (or narrator + intimacy DSP chain).
- **Speaker attribution**: BookNLP + light LLM → per-character casting from a
  voice bank; LitRPG status boxes → a flatter `system_ui` voice.
- **Per-series lexicon**: `surface → respelling` (+ optional IPA). Two layers —
  an always-on `data/lexicons/_base.csv` (names the g2p mangles everywhere) with
  `data/lexicons/<slug>.csv` stacked on top. Seed with `lexicon --write`, correct
  by ear once (`pron` shows phonemes + a rough gloss, before/after), cached forever.
- **Headings**: `"<Series>. Chapter N. <Title>"`, forced to end in sentence
  punctuation — Kokoro clips the final word of an unterminated line.

## Audio post

Per-voice EQ/comp; `thought` voice gets high-pass + gentle comp + a touch of
short reverb. Pauses: ~260 ms sentence, ~380 ms paragraph, ~1100 ms scene break,
+220 ms on a trailing ellipsis. Loudness: two-pass ffmpeg `loudnorm` to
−19 LUFS / −3 dBTP. Encode: Opus 56 kbit/s mono; optional M4B per arc with
chapter markers.

## Delivery

Generate a per-series podcast RSS feed; serve it on the LAN / Tailscale; any
phone podcast app subscribes, auto-downloads new chapters, tracks position.
M4B export stays available for single-file arcs.

## RoyalRoad sync (lower priority)

No official API. Log in via browser once, export the session cookie, use it with
`httpx`. Parse `div.chapter-content`, drop `.author-note` and hidden nodes. Be
polite (1 req / 2–3 s, cache, back off on 429). SQLite tracks
series/chapter/status; a `sync` command enqueues new chapters; a systemd-user
timer runs it nightly. Personal use only — respect authors with official audio.

## Roadmap

- **Phase 1 — spine** *(done)*: txt → Kokoro → Opus, config, lexicon, segment
  cache, `inspect`.
- **Phase 2 — pre-pass** *(done)*: `ingest.py` turns a saved `.html` or a live
  chapter URL into the same Block list; Royal Road anti-piracy decoys (a
  `display:none` stylesheet rule + a matching node) are stripped; italic
  character ranges are preserved and `build_segments` routes per-sentence italic
  runs to the `thought` voice with a light FFT band-limit/level DSP; richer
  spoken-form normalization (decimals, `%`, `e.g.`, spaced ellipsis, `#N`,
  chapter-heading phrasing) plus a `normalize_system` pass for stat lines;
  chapter/fiction titles become a spoken heading + Opus tags; `lexicon --write`
  appends candidate rows. New CLI: `fetch`, `lexicon`; `render`/`inspect` accept
  txt | html | url.
- **Phase 3 — cast** *(done)*: `dialogue.py` splits quotes out of each paragraph
  and attributes a speaker (explicit `"...," said X` / `X said, "..."` tags,
  pronoun + a running gender guess, sticky speaker for untagged continuations,
  descriptive referents → a lowercase key like `crone`). `[cast.voices]` maps
  names to Kokoro voice ids; `[cast] protagonist` catches untagged first-person
  lines. `[dsp.*]` effect chains (pitch via OLA, gain, HP/LP, spectral tilt) are
  applied after the segment cache, keyed speaker → voice → style; `thought` and
  `system` ship with sensible defaults. `system_ui` voice is wired (no system
  boxes in the sample chapter). New CLI: `cast`. **Deviation from the original
  plan:** BookNLP was dropped in favour of rules — no 2 GB dependency / model
  download, fully offline and deterministic, and every decision is visible in
  `inspect` and overridable in config. `Attributor.attribute` is the seam if a
  BookNLP/LLM backend is wanted later.
- **Livestream chat** *(done)*: `ingest._parse_chat` recognises
  `[Handle (Location): message]` (tolerates `[[`, a colon in the handle, missing
  location) and emits `Block("chat", message, meta={user, location})`; bracketed
  lines with no handle head, or a handle in `_SYSTEM_KEYWORDS`, fall through to
  `system`. `build_segments` routes chat to a rotating `[chat.voices]` pool
  (greedy distinct assignment in first-appearance order, then hash), style
  `chat`, rate `[chat] rate`, `[dsp.chat]` band-limit; inserts a `kind="cue"`
  earcon (`audio.earcon`, synthesised in numpy) before each run; speaks the
  handle per `[chat] speak_username`. `normalize_username` / `normalize_chat_message`
  handle CamelCase/digit handles, `@`-mentions, unicode emoji, ALL-CAPS.

- **Phase 4 — sync** *(done)*: `royalroad.py` (`RRClient` with a politeness
  delay + retry + optional stored cookie; `parse_fiction` reads the authoritative
  `window.chapters` JSON via a string-aware bracket scanner, falls back to the
  `#chapters` table). `db.py` is a small SQLite layer (`series`, `chapters` with
  per-chapter `status`). `sync.py`: `add_series` (with a
  `--from latest|start|N|<chapter-url>` skip marker), `refresh`, `run_sync`
  (oldest first, caches raw HTML under `library/<slug>/.raw/`, calls
  `pipeline.render`, keeps going past errors). CLI: `series
  add|refresh|list`, `sync`, `login` (cookie header or Netscape cookies.txt →
  `~/.config/webnovel-audio/session.json`), `schedule` (emits/installs a
  systemd-user `.service` + `.timer`).
- **Readable text output** *(done)*: `textout.render_markdown(doc)` turns the
  ingest `Document` into a Markdown file written next to every `.opus`
  (`NNN-<slug>.md`), and `render` does the same for one-off HTML/URL input. It's
  the distilled story — decoys gone, structure recovered (`#`/`* * *`/`> `/chat
  lines), italic spans → `*…*` — *before* spoken-form normalization, so numbers
  and names read as written. YAML front-matter carries provenance (`source`,
  `royalroad_id`, `retrieved`, `raw_sha256`, `raw_bytes`, `chapter`, `published`);
  `pipeline.load_document` fills the hash/size/timestamp onto `Document`. First-
  class archive + direct `pandoc … -o epub` source. `.txt` input produces no
  `.md` (it already is the text).

- **Phase 5 — delivery** *(done)*: `feed.py` builds RSS 2.0 + iTunes tags from
  the library DB (per-item enclosure with byte length + MIME from extension,
  `<itunes:duration>` from the stored `chapters.duration_s`, `pubDate` from the
  Royal Road publish date so a backlog sorts right, newest-first). `serve.py` is
  a stdlib `ThreadingHTTPServer`: `/` = a subscribe page listing tracked series,
  `/feed/<slug>.xml` = live feed, `/audio/...` and `/cover/...` = files with
  single-range HTTP support. `package.py` builds a `.m4b` via `ffmpeg` concat +
  an `;FFMETADATA1` chapters file (cumulative START/END from durations) + AAC
  transcode + embedded cover. CLI: `serve`, `book [--from N] [--to M]`, `feed`
  (static files for an external server). `sync` now caches `cover.jpg` per series
  and records each chapter's duration.
- **Phase 6 — premium (opt)**: overnight XTTS-v2 / StyleTTS2 narrator, A/B.

- **Tooling** *(done)*: `pron <text>` — the exact phoneme string Kokoro will use
  (via `kokoro_onnx.Tokenizer`, no model load) plus a rough ASCII gloss, raw and
  after the lexicon; `--check` audits every lexicon row. `voices` lists the 28
  ids; `voices --demo` renders one chaptered `.opus` (one chapter per voice, an
  announcer reads the id, then that voice reads a sample) — `write_opus` grew an
  optional `chapters=` arg that muxes an `;FFMETADATA1` file. `ui` launches the
  Tcl/Tk control app (execs `wish`, else Python's bundled Tk). Bare
  `webnovel-audio` with no subcommand prints `--help` rather than guessing.

- **`check` / cast seeding** *(done)*: `sync.suggest_cast` samples a chapter
  range (default `[cast] seed_chapters`), concatenates their blocks and runs
  `dialogue.discover` + the shared `dialogue.suggest_voices` (moved out of
  `cli` so `sync` need not import from it). With `--write` it writes
  `data/series/<slug>.toml`; re-run on a later range and it **only appends**
  speakers not already mapped (`_append_cast_voices` splices into the existing
  `[cast.voices]` table, leaving everything else in the file byte-identical), so
  a curated cast survives. The same pass reports `heteronyms.find_heteronyms`
  hits with context — never auto-corrected, since the right reading changes per
  sentence; a multi-word lexicon surface (`a tear in`) pins one where it matters.
  Entirely best-effort: any fetch/parse/write failure is logged, never raised.

## Royal Road ingest notes

- Chapter body: `div.chapter-content` (fallbacks: `.chapter-inner`, densest
  `<div>` of `<p>`).
- Anti-piracy: RR adds `<style>.<rand>{display:none}</style>` and drops a
  `<p>`/`<span class="<rand>">` with a "report this to Amazon" sentence into the
  body. `_hidden_class_names` scans every stylesheet for rules that hide content
  and `_strip_hidden` decomposes matches (plus inline `display:none`, `hidden`,
  `aria-hidden`, and `author-note` blocks) before text extraction.
- Each `<p>` carries a unique random watermark class — ignored.
- Italics (`<em>`/`<i>`): recorded as `(start, end)` char ranges on the raw block
  text; the segmenter dequotes length-preservingly, splits into sentences with
  offsets, and thresholds italic coverage per sentence.
- Metadata from `og:title` (`"<chapter> - <fiction>"`), first `<h1>`, `og:url`,
  and the "Next Chapter" link.

## Security model

Everything the tool ingests — chapter HTML, the `window.chapters` JSON, `og:*`
tags, cover URLs — is **attacker-influenced**: a hostile fiction author controls
their own page. The threat model is "a fiction you track is malicious," plus the
`serve` HTTP surface.

Handled:

- **HTML parsing** — only `BeautifulSoup(html, "lxml")` (the HTML parser, no DTD /
  entity resolution → no XXE); `lxml.etree` is never given untrusted input.
  `script`/`style`/`iframe`/`object`/`embed`/`svg`/`template`/`noscript` are
  decomposed before text extraction so nothing executable reaches the `.md`.
- **Path traversal** — `royalroad._safe_id` (`^\d{1,12}$`) and `_safe_slug`
  (`[A-Za-z0-9._-]`, no leading `.`/`-`, single path component) sanitise every id
  and slug at parse time; `sync` re-sanitises from the DB before building paths;
  `serve._file` resolves `realpath` and checks `commonpath` against the library
  root, rejects `NUL`.
- **XSS in `serve`'s index** — `_h()` escapes `& < > " '` for every interpolated
  value (title, author, slug, URL); the client `Host` header is stripped to
  host/port characters before use.
- **Session-cookie exfiltration** — covers come from an attacker's `og:image`;
  `royalroad.fetch_asset` fetches them with **no cookies** and only from
  `royalroad.com` / `royalroadcdn.com`.
- **Command injection** — `ffmpeg` is always invoked as an argv list (no
  `shell=True`); chapter titles go into an `ffmetadata` file with `=`/`;`/newline
  sanitised, never argv.
- **YAML front-matter** — control chars / newlines in titles are collapsed so a
  crafted title can't break the `.md` header.

Residual / accepted (single-user desktop tool):

- **SSRF via redirects** — `follow_redirects=True` on chapter fetches could be
  bounced to `localhost`/link-local. Low value here (no cloud metadata endpoint,
  the user picks each fiction URL); not currently restricted.
- **`serve` has no auth or CSRF protection.** It is read-only today. Any
  write endpoints (the planned add-series / schedule UI) MUST bind localhost
  only, carry CSRF tokens, and check `Origin`/`Host` to defeat DNS rebinding —
  or be a separate local GUI that calls the library functions directly.
- `schedule --calendar` is interpolated into a unit file; it's the user's own CLI
  argument, not remote input.

## Provider seam & the `--json` contract

Content acquisition is a `providers.Provider` (see `docs/PROVIDERS.md`): given a
source, produce an `ingest.Document`. `pipeline.load_document` and every `sync`
entry point resolve through `providers.resolve` / `resolve_series`, so Royal Road
and local `.html` are just the two shipped implementations; `.txt` bypasses the
registry (it's already the artifact). `textout.render_markdown(doc)` is the
canonical serialised form — the `library/<slug>/NNN-*.md` files — and the reason
the Markdown output exists: it's the stable boundary a future webnovel.com or
mbox provider only has to reach.

`series list|add|set|refresh`, `sync`, and `config` accept `--json`. `sync --json`
emits newline-delimited events (`start` / `series` / `chapter_begin` / `chapter` /
`done`) via `run_sync(emit=…)`. `config --json` reports resolved paths so a
front end is self-configuring. `ui/control.tcl` is exactly that: a Tcl/Tk app
that shells the CLI, parses the JSON (bundled `ui/json.tcl`), streams `sync`, and
carries its own interval/daily scheduler (`after`-based) as an alternative to the
systemd timer. It needs no `sqlite3`/`json`/`tls` Tcl packages — just `tk`.

The DB opens WAL + a busy timeout so a reader (a second CLI, a future native UI)
never blocks the `sync` writer.

## Per-series config overlays

`config.toml` is the base; `data/series/<slug>.toml` (dir = `[general]
series_config_dir`) is merged over it by `sync` via `Config.overlay` — only keys
present in the overlay change, `[cast.voices]`/`[chat.voices]` replace their
table, `[dsp.*]` merges per key. `_series_cfg` applies the auto per-series lexicon
first, then the overlay (which can still override `[general] lexicon`).

Lexicons stack: `pipeline._load_lexicon` loads `[general] base_lexicon`
(`data/lexicons/_base.csv` — standard names the g2p mangles) then the per-series
CSV via `Lexicon.load_many`, so a per-series row overrides the base for the same
`surface`. `inspect` / `lexicon --write` treat base entries as already-known.

## Buffering note

`sync --json` and `serve` set `sys.stdout` line-buffered and flush each event —
Python block-buffers a pipe, so without it the control UI (which reads their
stdout through a Tcl pipe) sees nothing until the child exits. The UI's
Feed-server button spawns `serve`, pipes its output to the log as `[serve] …`,
and stops it with `SIGINT` (clean `httpd.server_close()`), `SIGKILL` after 1 s.

## The 0.2 pipeline

`fetch` -> `parse` -> `check` -> `render` are separable stages over the same
`<target> [range]` signature, driven by one engine (`sync.run_stage` ->
`_advance`). Measured on the sample chapter: parse 28 ms, segment 3.6 ms, synth
35.3 s (88%), master+opus 4.9 s (12%) — so everything before `render` is free,
and the point of splitting them is to let configuration converge while iteration
costs milliseconds. `fetch`'s cost is politeness (`request_delay`), not compute.

**Range mood.** An explicit range is imperative (do exactly these, whatever their
status); no range is declarative (do what's outstanding). That's why `redo` is
gone — `render <slug> 20-30` *is* the re-render. Stages pull their own inputs, so
`render` on an unfetched chapter fetches and parses it first; `force` applies
only to the named stage, so a re-render never re-downloads.

**Chapter state is the only progress.** `status` walks new -> fetched -> parsed
-> rendered, plus `error` (with `error_stage`, so a render failure doesn't forget
it was fetched) and `skipped` (deliberately not wanted). There is no separate
reader-position marker and nothing syncs position to Royal Road — 0.1 had a
`progress_order` column doing two jobs (reader position *and* render high-water
mark), which made a `new` chapter behind the marker invisible to every command.
`--from N` now just marks 1..N `skipped`; `state set <slug> <range> skipped`
does it after the fact. 0.2.1 drops the column outright, folding its one real
meaning into chapter status first, so nothing becomes unreachable (an explicit
range ignores `skipped` anyway).

**Segmentation is deliberately not a stage.** It's 3.6 ms and derived from
blocks + config + lexicon + cast overlay — i.e. from exactly what the tuning loop
edits — so persisting it would invent a staleness class `fetch`/`parse` don't
have. It's recomputed each render and written beside the audio as
`.segments.json`, which makes that file the provenance of *that* render and a
diff baseline for "what would change if I re-rendered?".

**Provider seam for single-artifact sources.** `Provider.prefetch(fi, cfg,
cache_dir)` runs once per series before per-chapter `raw()` calls, and
`raw_ext` names the cached artifact's extension. Royal Road ignores both. A
Project Gutenberg `.txt` provider would download the whole book in `prefetch`,
split it into chapters, and serve `raw()` from local slices — so later fetches
never touch the network.

## Auth

A stored session cookie and nothing else. `login` parses cookies out of a
browser export and saves them 0600; `RRClient` attaches them to content fetches
so a subscriber-only chapter can be read. It is deliberately **not verified** —
0.2.0 checked by GETting `/my/follows`, which meant depending on an account page
whose markup drifts, and the answer was stale by the next request anyway. A bad
cookie surfaces where it's actionable instead: `_do_fetch` raises `ChapterLocked`
for a chapter whose `unlocked` flag is false, naming `login` and saying whether a
session exists at all. Nothing reads account state — the follows reader and
reading-position sync were removed in 0.2.1.

## Sync lock

`sync.sync_lock(cfg)` holds a non-blocking `flock` on `<state-db-dir>/sync.lock`
for the duration of a `sync` run — one state DB (one library) gets at most one
`sync` at a time. It's advisory and per-open-file-description, so it's released
automatically if the holder is killed; nothing to clean up by hand. A second
`sync` — from the UI, a second UI window, or a bare terminal invocation, doesn't
matter which — gets `SyncLocked` immediately: `_cmd_sync` turns that into a
`{"event": "locked", "path": ...}` line under `--json` (exit 2, distinct from
exit 1 for "ran, but some chapters errored") or a one-line message otherwise.
The UI's `$::RUNNING` guard still exists (it avoids even spawning a second
process from the same window) but the lock is what actually prevents two
processes from racing for the same 8 cores, which `$::RUNNING` alone couldn't.
