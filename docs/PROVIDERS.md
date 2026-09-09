# Content providers

A **provider** turns a *source* — a chapter URL, a saved file, later maybe a
mailbox or an API — into a canonical `ingest.Document`. Everything after that
(normalize → dialogue → segment → synth → audio, and the library / feed /
Markdown) works only on `Document`, so a new site is one class.

## The Document

`ingest.Document` is the stable in-memory contract:

| field | |
|---|---|
| `blocks` | ordered `Block(kind, text, italic, meta)` — `paragraph` / `heading` / `scene_break` / `system` / `chat`; `italic` is `(start,end)` char ranges; chat `meta` is `{user, location}` |
| `chapter_title`, `fiction_title`, `author`, `url`, `next_url` | metadata |
| `raw_sha256`, `raw_bytes`, `retrieved_at` | provenance (set by `providers.stamp_provenance`) |

`textout.render_markdown(doc)` is its canonical **serialised** form: readable
prose + YAML front-matter. The `library/<slug>/NNN-*.md` files are exactly this,
and are the intended long-term archive / ebook source. A provider only has to
produce a good `Document`; the Markdown, audio, feed, and `.m4b` come for free.

## Provider interface (`providers.py`)

```python
class Provider:
    name: str
    can_series: bool = False

    def handles(self, source: str) -> bool: ...
    def read(self, source: str, *, cfg) -> Document: ...   # one chapter

    # only if can_series:
    def series(self, source: str, *, cfg) -> royalroad.FictionInfo: ...
    def raw(self, url: str, *, cfg) -> str: ...            # chapter markup for sync's .raw/ cache
```

- `read` **must** call `providers.stamp_provenance(doc, raw_text, source_path=…)`.
- `series` returns a `FictionInfo` (`rr_id` = provider-native id, `slug`, `title`,
  `author`, `cover_url`, `url`, `provider`, `chapters=[ChapterRef…]`). `ChapterRef`
  ids/slugs are already run through `royalroad._safe_id` / `_safe_slug`; a new
  provider must do the same (they go straight into file paths).
- Register the instance in `providers.PROVIDERS`. `resolve(source)` picks the
  first that `handles` it; `None` means "treat as a plain `.txt` artifact".

Shipped: `RoyalRoadProvider` (URL, series-capable), `LocalHtmlProvider` (a saved
`.html`, chapter only). Plain `.txt` bypasses providers — it's already the text.

## Where the seams are

| concern | today | for a new provider |
|---|---|---|
| chapter fetch + parse | `RoyalRoadProvider.read` | implement `read` |
| fiction/index → chapter list | `RoyalRoadProvider.series` | implement `series` + `can_series=True` |
| `sync` raw-HTML cache | `prov.raw()` | implement `raw` |
| cover download | `sync._cache_cover` → `royalroad.fetch_asset` (RR hosts only) | override in the provider if the host differs |
| DB row | `series.provider` column (migrated in) | set `FictionInfo.provider` |
| auth | `royalroad.RRClient` + stored session cookie | provider's own client |

`pipeline.load_document` and all three `sync` entry points already go through
`providers.resolve` / `resolve_series`, so nothing else changes.

## Sketches (not implemented)

- **webnovel.com** — a `WebnovelProvider`: `handles` = `webnovel.com` URL;
  `read` fetches the chapter (their content is JSON-in-page or an API call),
  builds paragraph blocks, maps any "author's thoughts" / paragraph comments to
  `chat`-style blocks; `series` reads the table of contents. Their per-paragraph
  comment feature maps naturally onto the existing `chat` handling.
- **Usenet / mbox** — a `MailboxProvider`: `handles` = a `.mbox` path or
  `news:`/`nntp://`; `read` takes one article (or a whole group as a "series"),
  strips quoting/headers/sig blocks into paragraphs, treats `>` quote levels or
  follow-ups as they make sense. Front-matter carries `Message-ID`, `Newsgroups`,
  `Date`. This is a large part of why the Markdown output exists.
