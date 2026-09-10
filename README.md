# webnovel-audio

Turn web-serial fiction (Royal Road) into good multi-voice audiobooks, and keep a
clean text archive of every chapter while you're at it. Offline, batch, CPU-only —
no GPU, no cloud, no account required.

- **Multi-voice narration** — narrator + a distinct internal-monologue voice +
  per-character dialogue casting + a "system" voice for LitRPG status boxes + a
  band-limited voice pool for livestream "chat" overlays, each with its own
  post-processing chain.
- **Rule-based dialogue attribution** — explicit tags, pronoun + gender, sticky
  continuations, descriptive referents ("the old woman"). No model download; every
  decision is visible and overridable in config.
- **Library** — track a series, `sync` renders every new chapter past where you
  left off; drive it from a **Tcl/Tk control UI** or a systemd timer.
- **Provider seam** — Royal Road + local files today; a new source (webnovel.com,
  usenet/mbox, …) is one class producing the shared `Document`.
- **Three outputs per chapter** — mastered Opus, a **readable Markdown** copy
  (decoys stripped, structure kept, YAML provenance — feeds `pandoc … -o epub`),
  and the internal narration script.
- **Delivery** — a localhost/LAN server with a per-series **podcast RSS feed**
  (real dates, durations, cover art, HTTP Range), or a chapterised **`.m4b`**.
- **Faithful text handling** — anti-piracy decoy paragraphs removed, LitRPG
  number/stat normalization, per-series pronunciation lexicons.

Built for and tuned on an AMD Ryzen 7 8745HS / Radeon 780M laptop (no CUDA).
Rendering is not realtime — a ~2,800-word chapter is ~17 min of audio in ~3–5 min
wall. Architecture and design notes: [`docs/DESIGN.md`](docs/DESIGN.md).
Setup, deployment, automation, troubleshooting: [`SETUP.md`](SETUP.md).

## Requirements

- Linux, Python **3.12** (pinned; [`uv`](https://docs.astral.sh/uv/) fetches it)
- `ffmpeg` on `PATH`
- optional: `qrencode` (`serve` prints a scannable QR), `pandoc` (Markdown → EPUB)

## Quick start

```sh
git clone <this-repo> ~/Projects/webnovel-audio && cd ~/Projects/webnovel-audio
uv sync --extra kokoro                 # venv + deps + the Kokoro TTS engine
uv run webnovel-audio fetch-models     # ~400 MB, once
cp config.example.toml config.toml

# track a series and render the first few chapters
uv run webnovel-audio series add <royal-road-fiction-url> --from start
uv run webnovel-audio sync <series-slug> --limit 3

# serve the podcast feed to your phone / a podcast app
uv run webnovel-audio serve            # http://<this-machine>:8080/
```

`--from`: `start` | `latest` (you're caught up) | `42` (heard through ch. 42) |
`<chapter-url>`. Later, `webnovel-audio sync` renders everything new across all
tracked series; `webnovel-audio schedule --install` runs it nightly.

## Commands

| command | what |
|---|---|
| `render <txt\|html\|url> -o out.opus` | one chapter → `.opus` + `.md` + `.segments.json` |
| `inspect <input>` | how it parsed: blocks, styles, cast, thought routing, lexicon queue |
| `cast <input>` | detect speakers, print a `[cast.voices]` starter block |
| `lexicon <input> --write` | queue unknown proper nouns into `data/lexicons/<slug>.csv` |
| `series add\|list\|set\|refresh` | manage tracked series |
| `sync [key] [--limit N] [--dry-run]` | render new chapters into `library/` |
| `config` | show resolved paths (state DB, library, executable, …) |
| `login [--cookies-file … \| --check]` | store a Royal Road session cookie (optional) |
| `serve` | localhost/LAN podcast feeds + audio |
| `book <key> [--from N] [--to M]` | stitch chapters into a chapterised `.m4b` |
| `feed <key> --base-url URL` | write a static RSS file for an external web server |
| `schedule [--install]` | emit / install a systemd-user timer for nightly `sync` |
| `fetch <url>` / `fetch-models` | save a chapter page / download the TTS model |

`series list`, `series add/set/refresh`, `sync`, and `config` take **`--json`**
(machine-readable; `sync --json` streams one JSON event per line). Input to
`render`/`inspect`/`cast`/`lexicon` can be a chapter **URL**, a saved **`.html`**,
or a plain **`.txt`**.

### Control UI

```sh
wish ui/control.tcl        # needs tk (Arch: pacman -S tk)
```

A Tcl/Tk front end over the CLI: add/track series, run `sync` on demand or on an
in-app schedule (interval or daily), start/stop the feed **server**, with a live
log. It's a stand-alone alternative to the systemd timer — leave it open and it
drives the batch runs. Set `WEBNOVEL_AUDIO=/path/to/webnovel-audio` if it isn't
found automatically.

### Adding a content source

Royal Road and local `.html` are **providers** (`src/webnovel_audio/providers.py`);
plain `.txt` is passed through as-is. A new site (webnovel.com, an mbox, …) is one
`Provider` subclass that produces an `ingest.Document`; everything downstream —
audio, the readable `.md`, the feed, `.m4b` — is provider-agnostic. Contract and
sketches: [`docs/PROVIDERS.md`](docs/PROVIDERS.md).

Per-series overrides: drop `data/series/<slug>.toml` (any `config.toml` section) and `sync` merges it over the base config for that series — see [`data/series/README.md`](data/series/README.md).

## How it works

### Per-chapter outputs

`sync` (and `render` on HTML/URL) writes into `library/<slug>/`:

| file | |
|---|---|
| `NNN-<slug>.opus` | mastered mono Opus, −19 LUFS, tagged |
| `NNN-<slug>.md` | readable story text — decoys removed, structure recovered (`#` / `* * *` / `> ` / `[Handle: …]`), italics → `*…*`, **before** spoken-form rewriting; YAML front-matter with `source`, IDs, fetch time, a SHA-256 of the raw HTML |
| `NNN-<slug>.segments.json` | the internal narration script (voice/style/pause per line) |
| `.raw/<id>.html` | the raw fetched page (provenance; re-fetched if deleted) |

`pandoc library/<slug>/*.md -o book.epub` works today.

### Internal monologue

HTML ingest keeps italic character ranges. A sentence that is ≥
`synth.thought_threshold` (0.6) italic is routed to `voices.thought` and gets the
`[dsp.thought]` chain. Partial-paragraph italics split correctly — "He grimaced.
*Worth a shot.*" → one narration sentence + one thought sentence.

### Casting

`cast <chapter>` runs the attributor and prints `character → Kokoro voice id`
with line counts and a gender guess; paste into `config.toml`, adjust. Speakers
referred to only descriptively ("the old woman") become a lowercase key
(`woman`) you map like any other. `[cast] protagonist` catches untagged
first-person lines; unmapped speakers use `[cast] default`. Attribution is
rules-only and deterministic — it gets tags, pronoun tags, volleys and untagged
continuations right, and mis-assigns the occasional oddly-phrased line, which you
fix in the cast map.

### Livestream chat

`[Handle (Location): message]` lines (a livestream chat watching the protagonist)
become a `chat` style: each handle → a voice from `[chat.voices]` (first distinct
handles get distinct voices so an exchange never collides), the whole layer
band-limited via `[dsp.chat]` and read a bit faster, a soft earcon before each
run, the handle spoken once per chapter. Bracketed lines with **no** `Handle:`
head (`[Scan complete…]`) stay `system` — the in-fiction AI.

### Delivery

`serve` runs a `http.server` (localhost/LAN, read-only): `/` lists tracked series
with a copyable feed URL, `/feed/<slug>.xml` is a live RSS 2.0 + iTunes feed,
`/audio/…` and `/cover/…` serve files with single-range support. Opus-in-Ogg
works in AntennaPod / Podcast Addict / gPodder; Apple Podcasts and Overcast
don't — use `book` for a `.m4b`.

## Configuration

`config.toml` (copy from `config.example.toml`, auto-detected in the working
directory; `-c` for a different one):

`[general]` lexicon / per-series-config paths · `[voices]` fallback voices · `[cast]` + `[cast.voices]`
per-series casting · `[chat]` livestream-chat behaviour · `[synth]`
(`thought_threshold`, `system_rate`) · `[pauses]` · `[audio]` loudness ·
`[dsp.*]` effect chains keyed by speaker / voice / style · `[royalroad]`
(`library_dir`, `state_db`, `request_delay`) · `[serve]` (`host`, `port`) ·
`[book]` bitrate.

## Development

```sh
uv run pytest -q            # ~61 tests, fully offline
uv run python -m compileall -q src/
```

Layout: `providers` (source → `ingest` Document) → `normalize` / `dialogue` /
`segment` (blocks → narration script) → `synth/` (Kokoro or a silent `null`
backend) → `audio` (master + Opus) ; `textout` (Document → Markdown) ; `royalroad`
+ `db` + `sync` (library) ; `feed` + `serve` + `package` (delivery). `cli` wires
it together; `ui/` is a Tcl/Tk front end over the CLI.

## Status & scope

Personal project, still moving. It scrapes Royal Road for **personal listening
only** — respect authors who sell their own audiobooks, and the site's terms.
No warranty.

## License

[0BSD](LICENSE) (BSD Zero Clause) — do anything, no attribution or notice
required. (Swap in the Unlicense or CC0 if you prefer the public-domain framing;
0BSD is the same permissiveness with fewer jurisdictional questions.)
