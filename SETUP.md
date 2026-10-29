# Setup & deployment

Offline web-novel narration for personal listening. Everything runs on the CPU;
no GPU, no cloud, no account required (Royal Road is read through your own browser,
logged in or not as you like; section 9).

## 1. Requirements

| | |
|---|---|
| OS | Linux (developed on Arch/Omarchy; anything with Python + ffmpeg works) |
| Python | 3.12 or newer. `uv` prefers the one your distro ships (`python-preference = "system"` in `pyproject.toml`) — its Tk is built with Xft, which the control UI needs to render quotes and dashes. A uv-downloaded or conda Python is not, and `ui` will say so. |
| [`uv`](https://docs.astral.sh/uv/) | package/venv manager — `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
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

uv sync --extra kokoro           # .venv, deps, the Kokoro engine, and the
                                 # POS tagger + en_core_web_sm (render needs it)
uv run webnovel-audio models fetch   # ~400 MB, once, into ~/.cache/webnovel-audio
```

Verify:

```sh
uv run webnovel-audio --version
uv run pytest -q                 # ~240 tests, all offline (no network)
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
| `[library] library_dir` | where rendered chapters are written (default `library/` under the working dir) |
| `[library] state_db` | SQLite tracking file (default `~/.local/state/webnovel-audio/state.db`) |
| `[royalroad] request_delay`, `[scribblehub] request_delay` | seconds between requests to that site (be polite; default 2.5) |
| `[serve] port` / `base_url` / `tailnet_url` / `redirect_to_tailnet` | the feed server. `tailnet_url` adds a second (Tailscale) address and QR code at startup; `redirect_to_tailnet = true` temporarily 301s every feed on another host there, to move an existing podcast subscription. `access_log` (default `~/.local/state/webnovel-audio/access.log`, `""` = off) records who fetched what, rotating at about 4 MB |
| `[general] base_lexicon` | always-on respelling CSV (`data/lexicons/_base.csv`) applied to every series; a per-series row for the same word wins. `""` to disable |
| `[general] lexicon_dir` / `series_config_dir` | pre-bundle fallbacks, still read for a tree that has not been migrated |

Pass `-c /path/to/other.toml` to any command to use a different config. A
tracked series keeps its own `config.toml` and `lexicon.csv` inside its bundle
directory (`webnovel-audio series path --scope <slug>`), and `sync` merges that config
over the global one automatically — `cast edit <slug>` and `lex edit <slug>`
open them.

## 4. One-off render (sanity check)

Point `render` at a chapter URL, a saved `.html`, or a `.txt` file:

```sh
uv run webnovel-audio render --target samples/salvage-run-ch1.html -o out/test.opus

uv run webnovel-audio parse --target <same-input> --explain   # how it parsed, no audio
```

`out/test.opus` is mono Opus, loudness-normalised, with a `.segments.json` beside
it showing every line's voice/style/pause.

## 5. Library workflow (batch)

```sh
# start tracking a fiction; --from says where you already are
uv run webnovel-audio series add --url <royal-road-fiction-url>   # -n first to preview the slug
#   --from start   (default) nothing skipped
#   --from latest  you're caught up; skip everything that exists now
#   --from 42      you've already read through chapter 42
#   --from <chapter-url>

uv run webnovel-audio series list

# the cheap stages first, so you can configure before spending CPU
uv run webnovel-audio fetch --target <slug> --range 1-10       # ~2.5 s/chapter, politeness delay
uv run webnovel-audio parse --target <slug> --range 1-10       # ~30 ms/chapter
uv run webnovel-audio check --target <slug> --range 2-10       # report only, writes nothing
uv run webnovel-audio cast update --scope <slug> --range 2-10 # add the speakers it found
uv run webnovel-audio cast edit --scope <slug>        # tune the voices by ear
uv run webnovel-audio render --target <slug> --range 1-10      # the expensive one

# then the steady state
uv run webnovel-audio sync --scope <series-slug> --limit 3       # cap this run
uv run webnovel-audio sync --dry-run                     # show what all series would do
uv run webnovel-audio sync                               # everything, every enabled series
```

`sync` estimates how much CPU the run will take (from the median duration of
what that series has already rendered) and asks before starting — `-y`/`--yes`
skips that, and it's skipped automatically when stdin isn't a terminal, so
scripted runs are unaffected. It works oldest-first and records failures as `error` (retried next run,
with `error_stage` naming what broke) without stopping the batch. A chapter's
`status` alone decides whether it's outstanding — `state set <slug> -40 skipped`
marks 1..40 as already-dealt-with, and `state show <slug>` prints the
per-chapter stage table.
Finished a series? `series disable <slug>` drops it out of `sync`.

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

The control UI's **Service…** button does the same as `service` below, in a dialog.
On startup `serve` prints a **scannable QR** of the URL (needs `qrencode`; point
your phone camera at it), one for the local address and, if `tailnet_url` is
set, one for Tailscale. Or open `http://<this-machine-ip>:8080/` by hand.

That page is the **web player**: browse every series, play in the browser with
lock-screen controls and automatic next-chapter, and see how many chapters are
new since you last listened. There is no login: the first visit asks "Who's
listening?" and you pick or add a name, or choose *Just browsing*. Positions
(where you are, and the furthest chapter you have reached) are stored per name
on the server, so they follow you across devices and across the LAN and
Tailscale addresses. Chrome or any Chromium browser plays the Opus files; Safari
may not. Its API is under `/api/` (`library`, `profiles`, `positions`).

To use a podcast app instead, open a series and expand **Podcast feed**: it has
the feed URL (on the address you opened the page from), *Add to AntennaPod*,
*Copy URL* and *View feed*. Use an app that supports **Opus** (AntennaPod,
Podcast Addict, gPodder; Apple Podcasts and Overcast do not — use `book` for
those). New chapters appear in the feed as `sync` renders them, dated to their
real Royal Road publish time so a backlog sorts into reading order. The server
supports HTTP Range, so seek/resume works.

Standalone QR helper (installed at `~/.local/bin/qr`, uses `qrencode` +
`img2sixel`): `qr <text>` for a block QR, `qr -s <text>` for a sixel image,
`qr -p out.png <text>` to save one. `some-command | qr` reads stdin.

To leave it running, install it as a user service, which starts at login and restarts
itself if it dies:

```sh
uv run webnovel-audio service install     # writes ~/.config/systemd/user/webnovel-audio.service
uv run webnovel-audio service enable      # start now and at every login
uv run webnovel-audio service status      # state, pid, URL, recent log if it is down
```

`service disable` stops it, `service restart` picks up new code, and `service remove`
deletes the unit. Stop any `serve` you started by hand first, since they share the port.
It starts at login rather than boot; `loginctl enable-linger $USER` makes it start at boot.
The control UI's **Service…** button shows its state and offers the same actions
(install and start, restart, disable, remove, open the player, view the log).

Open the port if you have a firewall: `sudo ufw allow 8080/tcp` (or equivalent).

## 7. Audiobook file

```sh
uv run webnovel-audio book --scope <series-slug>                 # whole series -> one .m4b
uv run webnovel-audio book --scope <series-slug> --range 1-3 -o out/arc1.m4b
```

Produces a single AAC `.m4b` with a chapter marker + title per chapter and the
cover embedded — for any audiobook player.

## 8. The control UI

```sh
uv run webnovel-audio ui     # needs tk (Arch: pacman -S tk), else falls back
                             # to Python's bundled Tcl/Tk with --python
```

Laid out like gitk: series across the top, chapters bottom-left, a
Cast / Lexicon / Log notebook bottom-right, status bar along the bottom.

- Select chapters (shift/ctrl) and **right-click** to **Play**, or to run
  fetch / parse / check / render over that selection, or mark skipped/new and
  clear errors. A scattered pick becomes one comma range, e.g.
  `render --target <slug> --range 1-3,7,20-21`. Double-click plays; set
  `WEBNOVEL_AUDIO_PLAYER` (e.g. `mpv --no-video`) to choose the player,
  otherwise `xdg-open` decides.
- A re-render that finishes suspiciously fast is usually a full cache hit —
  the log says `[194/194 segments cached]`. The per-segment cache is keyed on
  text+voice+style+rate+pitch, so re-rendering a chapter only synthesizes what
  changed. Mastering (loudness + Opus) is the fixed cost: ~6 s for a 25-minute
  chapter with `[audio] mastering = "fast"` (the default; ~30 s with
  `"loudnorm"`), and `master_jobs` chapters master at once while the next one
  synthesizes. A chapter whose segments and audio settings are unchanged since
  its last render is skipped outright (`render --force` re-renders anyway); one
  rendered before this existed is rendered once, then skippable.
- **Sync…** shows a CPU estimate before starting.
- Cast and Lexicon are edited in place, with an mtime guard so a file changed
  underneath (by `cast update`) is never silently overwritten. **Open in
  $EDITOR** hands off for real editing.
- The feed **server** starts/stops from the toolbar.
- Window size and sash positions persist to `~/.config/webnovel-audio/ui.conf`.

Only one `render`/`sync` runs at a time per library (a `flock`), so a command
launched here while one is already going is refused with a log line rather than
fighting for the CPU.

## 9. Royal Road: through your browser

Royal Road sits behind two bot checks (Anubis, Cloudflare) that plain HTTP cannot pass,
so this tool does not fetch its pages itself: adding a series, refreshing its chapter
list and fetching chapters all go through your own browser, with the page-grabber
extension in `extension/`. A logged-in browser also reads locked chapters. There is no
stored login and no cookie handling. (Replaying a browser's cookies was tried and does
not hold up: Cloudflare judges the connection, not just the cookie.) `sync` therefore
never contacts Royal Road: it renders what the browser has brought, and says how many
chapters are still to fetch. `grab` is the sync that fetches. ScribbleHub is still
fetched directly.

Install the extension once: `chrome://extensions` → Developer mode → **Load unpacked** →
the `extension/` folder's one subfolder. `webnovel-audio serve` (and `grab` while it runs)
host a page, `http://localhost:<port>/extension`, that says whether the extension is
installed in that browser and whether it is older than the bundled one, with an **Update
now** button (it reloads the extension from its folder, so it picks up files a `git pull`
changed) and a zip download for another browser profile. `grab` says in the log when the
browser has not answered within 20 seconds, or answers with an older version, and the page
the extension draws offers an update when the server has a newer one bundled. The
extension's options (`chrome://extensions` → Details → Extension options) have **Close the
tab when a fetch run finishes cleanly**, on by default: it waits a few seconds with a
**Keep open** button, then closes only the tab the run was using, never the only tab in a
window, and never after an error or a stop. Chrome cannot install an extension from a
web page, so the first install is the Load unpacked step.

### Saving pages by hand

If Royal Road's Cloudflare check turns the tool away (a 403 "Cloudflare challenge"
message, or the Anubis one), read the chapters in Chrome and hand the pages over:

1. `chrome://extensions` → turn on Developer mode → **Load unpacked** →
   `extension/royalroad-grabber/` in this repo.
2. Open the chapter you are up to on royalroad.com. A small panel appears at the
   bottom right: set a number and press **Start**. The tab saves this chapter and
   steps through **Next** by itself, a few seconds apart, as if you were reading,
   so Royal Road moves your reading position along too. It stops at the number,
   at the last chapter, on a page with no chapter text (a bot-check), or on **Stop**.
3. Pages land in `~/Downloads/webnovel-audio/`. Then:

   ```sh
   uv run webnovel-audio ingest              # files them into the raw cache
   uv run webnovel-audio sync                # parse and render as usual
   ```

   `ingest` matches each page to a tracked chapter by the ids inside the page (so
   a plain "Save page as" works too), refuses bot-check pages, and moves what it has
   dealt with to `done/`. A chapter you already hold is left alone unless `--force`.
   If it says a chapter is unknown, run `series refresh` first (or save that series'
   page, below).

The control UI's **Ingest** button does the `ingest` step (it lists each page's
result in the log and refreshes the lists). It also starts the extension for you. Right-click a chapter → **Open in
browser** (just the page) or **Open in browser and save the next 10** (the extension
starts at once and walks 10 chapters). Right-click a series → **Open source page and
save it**: the extension saves the series page, and `ingest` reads its chapter list
and status like `series refresh` would, and reports where the site has you up to.
The page opens in your default browser, which must be the one with the extension.

### Fetch, ingest and render in one loop: `grab`

```sh
uv run webnovel-audio grab                          # every enabled series, like sync
uv run webnovel-audio grab --scope intelligent-design --limit 10
uv run webnovel-audio series add --url https://www.royalroad.com/fiction/…   # through the browser too
```

Scope and limit work like `sync`'s: with no scope, every **enabled** series, in sync's
order, and a paused series is left entirely alone (its chapter list too); a named
series is worked on even if it is paused. `--limit` is chapters per series (default:
everything outstanding). In the UI, **Sync…** has a "fetch through the browser" option
(on by default when a Royal Road series is in scope), and a series' right-click menu has
**Fetch in browser…**. **Add series…** opens the page in the browser and takes the chapter
list from it on the first pass, so mark chapters skipped (or use "start from") for
anything you do not want fetched.

For each series, in turn, the browser is sent to its series page first (the chapter
list, and where the site has you up to), then to each chapter in order. `grab` runs a
small server on the loopback address only (a random port, a one-time token passed in the
page address after the `#`, nothing accepted from web pages, a protocol-version check).
The extension sends each page it loads; `grab` files it, renders that chapter, and only
when the render is finished tells the browser which chapter to load next, so fetching is
paced by rendering. `--ahead K` lets K chapters wait unrendered (default 0). Chapters an
earlier run fetched but never rendered are rendered first. It stops when every series is
done, on a page that is not a chapter (a bot-check), on Stop in the page or the UI, or
when the browser has been silent for `--timeout` seconds. It holds the same lock as
`sync`, so the two never run together. Pressing Stop in the UI ends it within about 15
seconds, once any chapter already rendering finishes. A chapter marked **skipped** is
never fetched.

Personal use only — respect authors who sell their own audiobooks.

## 10. Where things live

| path | what | safe to delete? |
|---|---|---|
| `config.toml` | your settings | no (that's your config) |
| `~/.local/state/webnovel-audio/state.db` | tracked series + per-chapter status | no (loses progress) |
| `library/<slug>/*.opus` + `.m4b` | rendered audio | yes (re-render with `render --target <slug> --range <range>`) |
| `library/<slug>/*.md` | readable story text (archive / ebook source) | yes, but it's the cheapest thing to keep |
| `library/<slug>/*.segments.json` | internal narration script | yes |
| `library/<slug>/.raw/*.html` | cached chapter HTML (provenance) | yes (re-fetched on next `sync`) |
| `.cache/segments/<backend>/*.wav` | per-sentence synth cache | yes (just re-synthesises) |
| `~/.cache/webnovel-audio/` | the Kokoro model files (~400 MB) | yes (re-run `models fetch`) |

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
- **Rendering dies with no traceback, mentioning `/home/runner/...`** — the
  process is *gone*, not raising:

  ```
  Error processing file '/home/runner/work/espeakng-loader/espeakng-loader/
  espeak-ng/_dynamic/share/espeak-ng-data/phontab': No such file or directory.
  ```

  Nobody has that path. espeak-ng keeps its data directory in a fixed 160-byte
  buffer; install the venv deep enough and the path stops fitting, espeak
  ignores the one it was handed, falls back to the directory it was *compiled*
  in on some CI machine, and calls `exit()`. Check it with:

  ```sh
  webnovel-audio config show | grep espeak
  ```

  `espeak_ok True` means it really phonemized (in a subprocess — that failure
  cannot be caught in-process). Rendering refuses outright past 120
  characters rather than risking it, and says so; move the project somewhere
  shorter and re-run `uv sync`. Symlinking a short name onto a deep directory
  does not help, because the limit applies to the resolved path. This bites
  hardest on Windows, where `C:\Users\<name>\AppData\Local\...` is already
  long before the venv.

  The 120 is deliberately well under espeak's real limit: it is a buffer
  overrun, so the boundary is not crisp — measured here, 139 characters
  worked on one run and failed on the next. espeak-ng raises the POSIX limit
  to `PATH_MAX` in releases after 1.52.0, and the check reads the bundled
  library's version and stops applying itself once one of those is
  installed.
- **`models fetch` fails** — network/proxy issue; the files are two GitHub-release
  URLs (see `src/webnovel_audio/synth/kokoro.py`), download them manually into
  `~/.cache/webnovel-audio/`.
- **a site returns 429** — raise that provider's `request_delay` (`[royalroad]`,
  `[scribblehub]`); `sync` also backs off and retries automatically.
- **`config: library_dir, state_db now belong under [library]`** — a config
  from before 0.3; move those two keys from `[royalroad]` into a `[library]`
  table.
- **`serve` prints a `ConnectionResetError` traceback when a phone connects** —
  harmless: phones open extra speculative / HTTPS-probe TCP connections and drop
  them. Silenced as of the current version; if you still see it, update. The feed
  itself is fine — check the podcast app actually added it (test from a laptop:
  `curl -sI http://<ip>:8080/feed/<slug>.xml` → `200`).
- **A chapter mis-attributes dialogue or mispronounces a name** — that's per
  series: `webnovel-audio check --target <slug> --range <range>` for a `[cast.voices]` starter,
  `webnovel-audio cast update --scope <slug> --range <range>` to queue pronunciations into the
  series' `lexicon.csv`. See `README.md`.
- **Fixing a pronunciation** — for a word that's wrong everywhere (e.g.
  `Montgomery`, `lapis`), add a row to `data/lexicons/_base.csv` — it applies to
  every series. For a name specific to one series, use that series'
  `<bundle>/lexicon.csv` (a row there overrides `_base.csv` for the same word);
  `webnovel-audio lex edit --scope <slug>` opens it, and `lex promote --scope <slug> --surface <word>`
  moves a row from there into the base file once it turns out to be general.
  One row per rule, `surface,pos,respell,notes`. Leave `pos` blank to apply
  always; set it (`NOUN`, `VERB`, `ADJ`, a Penn tag like `VBD`, or `TAG+lemma`)
  to pin one reading of a heteronym — `live,VERB,liv` vs `live,ADJ,lyve`. Most
  specific wins. Give a phonetic **respell** — lowercase sound-it-out syllables
  joined by hyphens (`Kaelith,,kay-lith`), *not* uppercase (the g2p reads `KAY`
  as letters). `sync` auto-loads both files. Check a respell before
  committing to it: `webnovel-audio pron Montgomery` prints the raw phonemes and a
  rough gloss, then the same after the lexicon; `webnovel-audio pron --check`
  audits every row (add `--series <slug>` for that series' file). The UI's
  `pron` from a terminal does the same. To hear the fix in chapters you already
  rendered, just name them: `webnovel-audio render --target <slug> --range 12-15`. An explicit
  range is imperative — it re-renders whatever the recorded state, so there's no
  separate "mark these dirty" step.
- **`sync` re-renders something you already have** — its status is `error` or its
  status says so. `state show <slug>` shows every chapter's stage;
  `state set <slug> <range> skipped` takes one out of the queue.
- **`sync` refuses immediately with "another sync is already running"** — only
  one `sync` runs at a time per state DB (a `flock` on `<state-db-dir>/sync.lock`),
  whether the other one was launched from the UI, a second UI window, or a
  terminal — this is what stops two batches racing for the same CPU. Wait for
  the other to finish (`ps -ef | grep 'webnovel-audio sync'`); the lock releases
  itself the instant that process exits, crash or not — nothing to clean up.
