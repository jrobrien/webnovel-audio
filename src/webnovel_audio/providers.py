"""Content-acquisition providers.

A provider turns a *source* (a chapter URL, a saved file, later maybe a mailbox
or an API) into a canonical `ingest.Document` — decoy-stripped, structure
recovered, provenance stamped. Everything downstream (normalize → segment →
synth → audio, and the library / feed / markdown) works only on `Document`, so
adding a new site means writing one `Provider` subclass and registering it.

The stable contract:
  * `handles(source) -> bool`           can this provider take this source?
  * `read(source, *, cfg) -> Document`  one chapter, with `raw_sha256` etc. set
  * `can_series` / `series(source)`     optional: a fiction/index page -> chapter list
  * `raw(url, *, cfg) -> str`           fetch chapter markup (used by `sync`'s cache)

`textout.render_markdown(doc)` is the canonical *serialised* form of a Document;
see docs/PROVIDERS.md.
"""
from __future__ import annotations

import hashlib
import os
import time

from .ingest import Document, parse_document

_URL_PREFIXES = ("http://", "https://")


def _is_url(s: str) -> bool:
    return s.startswith(_URL_PREFIXES)


def stamp_provenance(doc: Document, raw_text: str, *, source_path: str = "") -> Document:
    data = raw_text.encode("utf-8", "replace")
    doc.raw_sha256 = hashlib.sha256(data).hexdigest()
    doc.raw_bytes = len(data)
    if source_path and os.path.exists(source_path):
        doc.retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%S",
                                         time.localtime(os.path.getmtime(source_path)))
    else:
        doc.retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    return doc


class Provider:
    name = "base"
    can_series = False

    def handles(self, source: str) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def read(self, source: str, *, cfg) -> Document:  # pragma: no cover - abstract
        raise NotImplementedError

    def raw(self, url: str, *, cfg) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def series(self, source: str, *, cfg):  # pragma: no cover - abstract
        raise NotImplementedError


class RoyalRoadProvider(Provider):
    name = "royalroad"
    can_series = True

    def handles(self, source: str) -> bool:
        return _is_url(source) and "royalroad.com" in source

    def _client(self, cfg):
        from .royalroad import RRClient

        return RRClient(delay=cfg.royalroad.request_delay)

    def raw(self, url: str, *, cfg) -> str:
        client = self._client(cfg)
        try:
            return client.html(url)
        finally:
            client.close()

    def read(self, source: str, *, cfg) -> Document:
        html = self.raw(source, cfg=cfg)
        return stamp_provenance(parse_document(html, url=source), html)

    def series(self, source: str, *, cfg):
        from .royalroad import parse_fiction

        fi = parse_fiction(self.raw(source, cfg=cfg), url=source)
        fi.provider = self.name
        return fi


class LocalHtmlProvider(Provider):
    """A saved chapter page on disk — same parser as RoyalRoad, no fetching."""

    name = "local-html"

    def handles(self, source: str) -> bool:
        return not _is_url(source) and source.lower().endswith((".html", ".htm"))

    def read(self, source: str, *, cfg) -> Document:
        html = open(source, encoding="utf-8").read()
        return stamp_provenance(parse_document(html), html, source_path=source)


PROVIDERS: list[Provider] = [RoyalRoadProvider(), LocalHtmlProvider()]


def resolve(source: str) -> Provider | None:
    """First provider that handles `source`, or None (→ treat as plain text)."""
    for prov in PROVIDERS:
        if prov.handles(source):
            return prov
    return None


def resolve_series(source: str) -> Provider | None:
    prov = resolve(source)
    return prov if (prov and prov.can_series) else None
