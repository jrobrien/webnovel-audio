# Content providers

A **provider** turns a *source* — a series URL, a chapter URL, a saved file, a
single-file ebook — into canonical `document.Document`s. Everything after that
(normalize → dialogue → segment → synth → audio, and the library / feed /
Markdown) works only on `Document`, so a new site is one module.

**Rule: site knowledge lives in `src/webnovel_audio/providers/` and nowhere
else.** Core code (`sync`, `pipeline`, `feed`, `textout`, `config`, `db`,
`cli`, …) gets a provider from the registry and talks to it only through
`providers.base.Provider`. `tests/test_architecture.py` fails if a core module
names a site or imports a concrete provider module.

## Layout

```
providers/
  __init__.py     registry: resolve(source), resolve_series, get(name), for_raw(raw), context()
  base.py         Provider, ChapterRef, VolumeRef, SeriesInfo, FetchContext, ChapterLocked
  html.py         site-agnostic HTML -> Blocks toolkit (hidden-text stripping, italics,
                  chat/system lines, container guess); knows no site
  http.py         PoliteClient (per-host rate limit, retries), fetch_asset(url, hosts)
  session.py      CookieSession: stored cookies for a provider with logins
  royalroad.py    Royal Road
  scribblehub.py  ScribbleHub
  local_html.py   a saved page on disk (hands it to the site that recognises it)
```

## The Document

`document.Document` is the stable in-memory contract:

| field | |
|---|---|
| `blocks` | ordered `Block(kind, text, italic, meta)` — `paragraph` / `heading` / `scene_break` / `system` / `chat`; `italic` is `(start,end)` char ranges; chat `meta` is `{user, location}` |
| `chapter_title`, `fiction_title`, `author`, `url`, `next_url` | metadata |
| `raw_sha256`, `raw_bytes`, `retrieved_at` | provenance, set by `document.stamp_provenance` — by core, never by a provider |

`textout.render_markdown(doc)` is its canonical **serialised** form: readable
prose + YAML front-matter (`sync` adds `provider` and `source_id`).

## The interface

```python
class Provider:
    name: str                 # stored in series.provider; config table [<name>]
    guid_prefix: str          # feed GUIDs are f"{guid_prefix}-{source_id}"; never change it
    raw_ext = ".html"         # extension of the cached per-chapter artifact
    can_series = False
    session = None            # a CookieSession if the site has logged-in content

    def handles(self, source) -> bool
    def recognizes(self, raw) -> bool                    # "this saved page came from us"
    def series(self, source, ctx) -> SeriesInfo          # if can_series
    def check_fetchable(self, ref, ctx) -> None          # raise ChapterLocked with a hint
    def fetch(self, ref, ctx) -> str                     # raw artifact: network or local slice
    def parse(self, raw, ref, ctx) -> Document           # pure and offline
    def fetch_cover(self, url, ctx) -> bytes | None      # restrict to the provider's hosts
    def read(self, source, ctx) -> Document              # one-off: fetch + parse (default)
```

- **`parse` is dispatched by `series.provider`**, never by what a file path
  looks like. It must be deterministic: `parse` re-runs whenever a chapter is
  re-parsed or re-rendered from the cached raw artifact.
- **`fetch` takes the whole `ChapterRef`**, not a URL. A chapter may have no
  URL at all (slices of one file).
- **`ChapterRef.order` is the 0-based reading order** — normalise whatever the
  site uses. `source_id` must be path-safe (it names `.raw/<id><raw_ext>`); run
  ids and slugs through `safepath.safe_slug` or a stricter check of your own.
- **`SeriesInfo.source_id` is unique per provider**, not globally: the DB key is
  `(provider, source_id)`.

### FetchContext

| field | |
|---|---|
| `cfg` | the full Config (read-only by convention) |
| `settings` | this provider's own config table, `[<name>]` in `config.toml` (e.g. `request_delay`) |
| `raw_dir` | the series' raw-artifact cache (`<bundle>/.raw`); `""` at `series add` time and for one-offs |
| `state` | this series' opaque provider state, as last returned |
| `known` | the chapter list as last recorded, for incremental refreshes |

`series()` may return `SeriesInfo.state` (a JSON-able dict); core stores it in
`series.provider_state` and hands it back as `ctx.state` on every later call.
Core never looks inside. `None` leaves the stored state alone.

## Shipped providers

- **`royalroad`** — `window.chapters` JSON on the fiction page (visible table as
  fallback), volumes with their own covers, decoy-text stripping, optional
  login cookie for locked chapters. GUID prefix `rr`.
- **`scribblehub`** — the table of contents is paged (`?toc=N`, newest first,
  `Referer` required); `order` attribute is the reading order and the URL's
  chapter id is not. Refresh walks from the newest page and stops at the first
  page that agrees with `ctx.known`; a renumbering (e.g. a glossary inserted at
  the front) makes nothing agree, so the whole list is re-read. GUID prefix `sh`.
- **`local-html`** — a saved `.html` on disk, chapter only. Offers the page to
  each provider's `recognizes()` and uses that site's `parse`; otherwise a
  generic reading via `html.guess_content_container`.

Plain `.txt` bypasses providers — it's already the text.

## Adding one

1. `providers/<name>.py` with a `Provider` subclass; keep every selector, URL
   pattern and quirk in that module. Use `html.py` for the markup-to-blocks
   work and `http.PoliteClient` for requests (put the single network call in
   one method, e.g. `_get`, so tests can replace it).
2. Register an instance in `providers.PROVIDERS`.
3. Tests with **synthetic** pages that mimic the site's markup (see
   `tests/test_scribblehub.py`) — no real chapter text in the repo.
4. If the site needs settings, document the `[<name>]` table in
   `config.example.toml`.

### Sketch: a single-file ebook (Project Gutenberg)

Not implemented; the interface is shaped so it fits
(`tests/test_scribblehub.py::test_single_artifact_provider_contract` exercises
it with a toy):

- `handles` a `gutenberg.org/ebooks/<n>.txt.utf-8` URL; `raw_ext = ".txt"`.
- `series` downloads the file (into `ctx.raw_dir` when it is set), splits it at
  chapter headings, and returns `ChapterRef(source_id="ch-003", url="")` for
  each; any split overrides go in `SeriesInfo.state`.
- `fetch(ref, ctx)` reads the cached whole file (downloading once if missing)
  and returns the slice for `ref.order`; `parse` turns it into paragraphs.
