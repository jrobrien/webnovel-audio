"""Content providers: the only place that knows about any particular site.

A provider turns a *source* (a fiction URL, a chapter URL, a saved file, a
single-file ebook) into canonical `document.Document`s. Core code resolves a
provider here — by source for something new, by name for a tracked series —
and talks to it only through `base.Provider`. See docs/PROVIDERS.md.
"""
from __future__ import annotations

from .base import (ChapterLocked, ChapterRef, FetchContext, Provider, SeriesInfo,
                   VolumeRef)
from .local_html import LocalHtmlProvider
from .royalroad import RoyalRoadProvider
from .scribblehub import ScribbleHubProvider

__all__ = ["ChapterLocked", "ChapterRef", "FetchContext", "Provider", "SeriesInfo",
           "VolumeRef", "PROVIDERS", "resolve", "resolve_series", "get", "for_raw",
           "context", "with_session"]

PROVIDERS: list[Provider] = [RoyalRoadProvider(), ScribbleHubProvider(),
                             LocalHtmlProvider()]


def resolve(source: str) -> Provider | None:
    """First provider that handles `source`, or None (-> treat as plain text)."""
    for prov in PROVIDERS:
        if prov.handles(source):
            return prov
    return None


def resolve_series(source: str) -> Provider | None:
    prov = resolve(source)
    return prov if (prov and prov.can_series) else None


def get(name: str) -> Provider:
    """The provider a tracked series was added with."""
    for prov in PROVIDERS:
        if prov.name == name:
            return prov
    raise SystemExit(f"unknown content provider {name!r} "
                     f"(known: {', '.join(p.name for p in PROVIDERS)})")


def for_raw(raw: str) -> Provider | None:
    """The web provider that produced this saved artifact, if any."""
    for prov in PROVIDERS:
        if prov.recognizes(raw):
            return prov
    return None


def with_session() -> list[Provider]:
    return [p for p in PROVIDERS if p.session is not None]


def context(cfg, prov: Provider, *, raw_dir: str = "", state: dict | None = None) -> FetchContext:
    return FetchContext(cfg=cfg, settings=dict(cfg.providers.get(prov.name, {})),
                        raw_dir=raw_dir, state=dict(state or {}))
