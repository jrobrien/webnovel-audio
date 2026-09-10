# Setup & deployment

Offline web-novel narration for personal listening. Everything runs on the CPU;
no GPU, no cloud, no account required (a Royal Road login is optional, for
paywalled chapters only).

## 1. Requirements

| | |
|---|---|
| OS | Linux (developed on Arch/Omarchy; anything with Python + ffmpeg works) |
| Python | 3.12 (pinned in `.python-version`; `uv` fetches it for you) |
| [`uv`](https://docs.astral.sh/uv/) | package/venv manager — `curl -LsSf https://astral.sh/uv/install.sh | sh` |
| `ffmpeg` | on `PATH` (`ffmpeg -version`) — used for mastering, Opus, and `.m4b` |
| `qrencode` | optional — `serve` shows a scannable QR of the feed URL if present (`pacman -S qrencode`; `libsixel` too for `qr -s`) |
| Disk | ~400 MB for the Kokoro model, plus ~8 MB per rendered chapter |
| RAM | ~2 GB while rendering |

No CUDA/ROCm needed. On a modern CPU a chapter renders in roughly a third to a
fifth of its playback length.

## 2. Install

```sh
git clone <this repo> ~/Projects/webnovel-audio      # or copy it there
cd ~/Projects/webnovel-audio

uv sync --extra kokoro           # create .venv, install deps + the Kokoro engine
uv run webnovel-audio fetch-models   # ~400 MB, once, into ~/.cache/webnovel-audio
```

Verify:

```sh
uv run webnovel-audio --version
uv run pytest -q                 # ~61 tests, all offline
```

Every command below is `uv run webnovel-audio …`. If you'd rather type
`webnovel-audio …`, either `source .venv/bin/activate` or add
`~/Projects/webnovel-audio/.venv/bin` to your `PATH`.

## 3. Configure

```sh
cp config.example.toml config.toml     # auto-detected in the working directory
```

`config.toml` is grouped by area; the defaults are sensible. The settings you're
most likely to touch:

| key | meaning |
|---|---|
| `[synth] backend` | `kokoro` (real audio) or `null` (silent stand-in, for testing the pipeline fast) |
| `[voices] narrator` / `thought` / `system_ui` | default Kokoro voices (`webnovel-audio` has 54; English ids start `af_/am_/bf_/bm_`) |
| `[royalroad] library_dir` | where rendered chapters are written (default `library/` under the working dir) |
| `[royalroad] state_db` | SQLite tracking file (default `~/.local/state/webnovel-audio/state.db`) |
| `[royalroad] request_delay` | seconds between requests to royalroad.com (be polite; default 2.5) |
| `[serve] port` / `base_url` | the LAN feed server |
| `[general] lexicon_dir` | drop `data/lexicons/<series-slug>.csv` here and `sync` uses it automatically |
| `[general] base_lexicon` | always-on respelling CSV (`data/lexicons/_base.csv`) applied to every series; a per-series row for the same word wins. `""` to disable |
| `[general] series_config_dir` | drop `data/series/<series-slug>.toml` here to override any config section for one series |

Pass `-c /path/to/other.toml` to any command to use a different config. For
per-series tweaks (a cast, chat behaviour, pauses…) prefer a
`data/series/<slug>.toml` overlay — `sync` merges it over `config.toml`
automatically; see `data/series/README.md`.

## 4. One-off render (sanity check)

Point `render` at a chapter URL, a saved `.html`, or a `.txt` file:

```sh
uv run webnovel-audio render samples/salvage-run-ch1.html -o out/test.opus

uv run webnovel-audio inspect <same input>     # see how it parsed, no audio
```

`out/test.opus` is mono Opus, loudness-normalised, with a `.segments.json` beside
it showing every line's voice/style/pause.

## 5. Library workflow (batch)

```sh
# start tracking a fiction; --from says where you already are
uv run webnovel-audio series add <royal-road-fiction-url> --from start
#   --from start   render from chapter 1
#   --from latest  you're caught up; only future chapters
#   --from 42      you've heard through chapter 42
#   --from <chapter-url>

uv run webnovel-audio series list

# render the next chapters that are past your progress mark
uv run webnovel-audio sync <series-slug> --limit 3       # cap this run
uv run webnovel-audio sync --dry-run                     # show what all series would do
uv run webnovel-audio sync                               # everything, every series
```

`sync` fetches oldest-first, advances your progress **only on success**, and
records failures as `error` (retried next run) without stopping the batch.
Re-point progress any time with `series set <slug> <start|latest|N|url>`.

Each chapter writes three files into `library/<slug>/`:

| file | what |
|---|---|
| `NNN-<slug>.opus` | the narrated audio |
| `NNN-<slug>.md` | **the readable story text** — decoys removed, structure recovered, *before* any spoken-form rewriting; italics kept, YAML front-matter with source URL / IDs / fetch time / a SHA-256 of the raw HTML |
| `NNN-<slug>.segments.json` | the internal narration script (voice/style/pause per line) |

plus the raw fetched HTML cached in `library/<slug>/.raw/` (provenance; re-fetched
if deleted).

The `.md` files are a clean archive and a direct ebook source —
`pandoc library/<slug>/*.md -o book.epub` works today (metadata comes from the
front-matter).

## 6. Serve to your phone

```sh
uv run webnovel-audio serve            # binds 0.0.0.0:8080 by default
```

You can also start/stop the server from the control UI (Feed server → Start).
On startup `serve` prints a **scannable QR** of the URL (needs `qrencode`; point
your phone camera at it). Or open `http://<this-machine-ip>:8080/` by hand — it
lists each tracked series with a feed URL. Add that feed URL in a podcast app
that supports **Opus** (AntennaPod, Podcast Addict, gPodder; Apple Podcasts and
Overcast do not — use `book` for those). New chapters appear in the feed as
`sync` renders them, dated to their real Royal Road publish time so a backlog
sorts into reading order. The server supports HTTP Range, so seek/resume works.

Standalone QR helper (installed at `~/.local/bin/qr`, uses `qrencode` +
`img2sixel`): `qr <text>` for a block QR, `qr -s <text>` for a sixel image,
`qr -p out.png <text>` to save one. `some-command | qr` reads stdin.

To run it in the background permanently, use the same systemd approach as §8 with
`ExecStart=…/.venv/bin/webnovel-audio serve` and a `[Install]`/`WantedBy` in a
`.service` (no timer).

Open the port if you have a firewall: `sudo ufw allow 8080/tcp` (or equivalent).

## 7. Audiobook file

```sh
uv run webnovel-audio book <series-slug>                 # whole series -> one .m4b
uv run webnovel-audio book <series-slug> --from 1 --to 3 -o out/arc1.m4b
```

Produces a single AAC `.m4b` with a chapter marker + title per chapter and the
cover embedded — for any audiobook player.

## 8. Run it: control UI or systemd timer

### Option A — the Tcl/Tk control UI

```sh
uv run webnovel-audio ui     # or just `uv run webnovel-audio`
```

`ui` runs `wish` if it's installed (`sudo pacman -S tk`), else Python's bundled
Tcl/Tk (`--python` forces the fallback); either way it sets `WEBNOVEL_AUDIO` so
the UI finds the CLI. You can still run `wish ui/control.tcl` directly from the
project root (add `WEBNOVEL_AUDIO=$(pwd)/.venv/bin/webnovel-audio` if it isn't
found).

Add series, run `sync` on demand (all / selected, with a chapter limit), or turn
on **Auto-sync** (every N minutes, or daily at a time) and leave the window open
— it becomes the thing that launches your batch runs, with a live log. Settings
persist to `~/.config/webnovel-audio/ui.conf`.

The **Edit lexicon / overrides / config / base lexicon** buttons open files with
`xdg-open`. If that picks the wrong app — a common one: an empty `.csv` is sniffed
as `text/plain` (text editor) but a populated one as `text/csv` (spreadsheet), so
two lexicons open differently — either fix the association
(`xdg-mime default nvim.desktop text/csv application/csv text/x-csv`) or set
`WEBNOVEL_AUDIO_EDITOR` before launching `wish` (a command with optional args:
`WEBNOVEL_AUDIO_EDITOR="$EDITOR"`, `="foot -e nvim"`, `="code -w"`).

### Option B — a systemd-user timer (unattended, headless)

### the timer

```sh
uv run webnovel-audio schedule --install     # writes ~/.config/systemd/user/*
systemctl --user daemon-reload
systemctl --user enable --now webnovel-audio-sync.timer
systemctl --user list-timers | grep webnovel   # check next run
journalctl --user -u webnovel-audio-sync -f    # watch a run
```

Default schedule is 03:00 daily (`--calendar '*-*-* 03:00'` to change). For the
timer to fire while you're logged out: `loginctl enable-linger $USER`.

## 9. Royal Road login (optional)

Only needed for chapters that require an account (adult content, some
early-access). Credentials are never stored — only the session cookie.

1. In your browser, log in to royalroad.com and export cookies for the site
   (a "cookies.txt" browser extension, or devtools → copy the `Cookie` header).
2. ```sh
   uv run webnovel-audio login --cookies-file ~/Downloads/cookies.txt
   # or:  uv run webnovel-audio login --cookie "name=value; name2=value2"
   uv run webnovel-audio login --check
   ```

Stored at `~/.config/webnovel-audio/session.json` (mode 600). `login --logout`
forgets it. Personal use only — respect authors who sell their own audiobooks.

## 10. Where things live

| path | what | safe to delete? |
|---|---|---|
| `config.toml` | your settings | no (that's your config) |
| `~/.local/state/webnovel-audio/state.db` | tracked series + per-chapter status | no (loses progress) |
| `library/<slug>/*.opus` + `.m4b` | rendered audio | yes (re-render with `sync` after `series set … start`) |
| `library/<slug>/*.md` | readable story text (archive / ebook source) | yes, but it's the cheapest thing to keep |
| `library/<slug>/*.segments.json` | internal narration script | yes |
| `library/<slug>/.raw/*.html` | cached chapter HTML (provenance) | yes (re-fetched on next `sync`) |
| `.cache/segments/<backend>/*.wav` | per-sentence synth cache | yes (just re-synthesises) |
| `~/.cache/webnovel-audio/` | the Kokoro model files (~400 MB) | yes (re-`fetch-models`) |
| `~/.config/webnovel-audio/session.json` | RR session cookie | yes (re-`login`) |

## 11. Update / uninstall

```sh
git pull && uv sync --extra kokoro     # update
```

Uninstall = delete the repo directory plus the four `~/.{cache,config,local}`
paths above. Nothing else is touched; no system packages are installed.

## 12. Troubleshooting

- **Podcast app won't play the audio** — it doesn't support Opus. Use AntennaPod
  / Podcast Addict, or make a `.m4b` with `book`.
- **`ffmpeg not found`** — install it (`sudo pacman -S ffmpeg`).
- **`fetch-models` fails** — network/proxy issue; the files are two GitHub-release
  URLs (see `src/webnovel_audio/synth/kokoro.py`), download them manually into
  `~/.cache/webnovel-audio/`.
- **royalroad.com returns 429** — raise `[royalroad] request_delay`; `sync` also
  backs off and retries automatically.
- **`serve` prints a `ConnectionResetError` traceback when a phone connects** —
  harmless: phones open extra speculative / HTTPS-probe TCP connections and drop
  them. Silenced as of the current version; if you still see it, update. The feed
  itself is fine — check the podcast app actually added it (test from a laptop:
  `curl -sI http://<ip>:8080/feed/<slug>.xml` → `200`).
- **A chapter mis-attributes dialogue or mispronounces a name** — that's per
  series: `webnovel-audio cast <chapter-url>` for a `[cast.voices]` starter,
  `webnovel-audio lexicon <chapter-url> --write` to queue pronunciations into
  `data/lexicons/<slug>.csv`. See `README.md`.
- **Fixing a pronunciation** — for a name that's wrong everywhere (e.g.
  `Montgomery`, `Eleanor`), add a row to `data/lexicons/_base.csv` — it applies to
  every series. For a name specific to one series, use `data/lexicons/<slug>.csv`
  (a row here overrides `_base.csv` for the same word). One row per term,
  `surface,respell,ipa,notes`; give a phonetic **respell** — lowercase
  sound-it-out syllables joined by hyphens (`Kaelith,kay-lith`), *not* uppercase
  (the g2p reads `KAY` as letters). The `ipa` column is reserved and ignored
  today; leave it blank. `sync` auto-loads both files. Check a respell before
  committing to it: `webnovel-audio pron Montgomery` prints the raw phonemes and a
  rough gloss, then the same after the lexicon; `webnovel-audio pron --check`
  audits every row (add `--series <slug>` for that series' file). The UI's
  **Test word…** button does the same. To hear the fix in chapters you already
  rendered: `webnovel-audio series redo <slug> [N | N-M]` (blank = every rendered
  chapter) marks them back to *pending* and rewinds `progress_order`; then
  `webnovel-audio sync <slug>`. The control UI does the same from **Selected
  series → Edit lexicon** and **Re-render…**.
- **`sync` re-renders something you already have** — its status is `error` or its
  `ord` is past your `progress_order`. `series list` shows the mark;
  `series set <slug> N` fixes it.
