"""A saved chapter page on disk. No fetching.

If a web provider recognises the page (it was saved from that site) its own
parser reads it; otherwise a generic best-effort reading is used.
"""
from __future__ import annotations

from ..document import Document, stamp_provenance
from . import html as H
from .base import ChapterRef, FetchContext, Provider


def parse_generic(raw: str, *, url: str = "") -> Document:
    soup = H.soup_of(raw)
    content = H.guess_content_container(soup)
    if content is None:
        raise SystemExit("could not locate chapter content in the page")
    H.strip_hidden(content, H.hidden_class_names(soup))
    title = H.meta_content(soup, "og:title", "twitter:title")
    if not title:
        h = soup.find("h1") or soup.find("title")
        title = h.get_text(" ", strip=True) if h else ""
    return Document(blocks=H.blocks_from_container(content), chapter_title=title,
                    url=url or H.meta_content(soup, "og:url"),
                    next_url=H.next_link_by_text(soup))


class LocalHtmlProvider(Provider):
    name = "local-html"

    def handles(self, source: str) -> bool:
        return (not source.startswith(("http://", "https://"))
                and source.lower().endswith((".html", ".htm")))

    def fetch(self, ref: ChapterRef, ctx: FetchContext) -> str:
        return open(ref.url, encoding="utf-8").read()

    def parse(self, raw: str, ref: ChapterRef, ctx: FetchContext) -> Document:
        from . import for_raw
        site = for_raw(raw)
        if site is not None:
            return site.parse(raw, ChapterRef(source_id="", order=0, title="", slug=""), ctx)
        return parse_generic(raw)

    def read(self, source: str, ctx: FetchContext) -> Document:
        ref = self.ref_for(source)
        raw = self.fetch(ref, ctx)
        return stamp_provenance(self.parse(raw, ref, ctx), raw, source_path=source)
