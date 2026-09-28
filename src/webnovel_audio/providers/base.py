"""The provider contract: what a content source must implement, and the
neutral records it hands back.

Everything here is site-agnostic. Site knowledge (URLs, markup, auth, how a
chapter list is paged) lives only in the concrete provider modules; the core
(`sync`, `pipeline`, `feed`, …) talks to a provider through this interface and
never names a site. `tests/test_architecture.py` enforces that.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..document import Document, stamp_provenance


@dataclass
class ChapterRef:
    """One chapter as the source lists it.

    `order` is the 0-based reading position; the provider normalises whatever
    the site uses. `source_id` is the provider's own stable id and must be
    path-safe (it names the raw-cache file). `url` may be empty for a source
    whose chapters are slices of one artifact.
    """
    source_id: str
    order: int
    title: str
    slug: str
    url: str = ""
    published_at: str = ""
    unlocked: bool = True
    volume_id: str = ""       # "" is normal: many chapters are in no volume


@dataclass
class VolumeRef:
    source_id: str
    title: str
    cover_url: str = ""
    order: int = 0


@dataclass
class SeriesInfo:
    source_id: str
    slug: str
    title: str
    author: str = ""
    author_url: str = ""
    cover_url: str = ""
    url: str = ""
    chapters: list[ChapterRef] = field(default_factory=list)
    volumes: list[VolumeRef] = field(default_factory=list)
    provider: str = ""
    tags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str = ""
    rating: float = 0.0
    #: the provider's per-series state to persist; None leaves it unchanged
    state: dict | None = None


@dataclass
class FetchContext:
    """What a provider gets to work with for one series (or a one-off source).

    `settings` is the provider's own config table (`[<provider name>]`),
    `raw_dir` the series' raw-artifact cache ("" for a one-off), and `state`
    the opaque per-series JSON the provider returned last time. Core stores
    `state` and never looks inside it. `known` is the chapter list as last
    recorded, so a provider can refresh incrementally.
    """
    cfg: object
    settings: dict = field(default_factory=dict)
    raw_dir: str = ""
    state: dict = field(default_factory=dict)
    known: list[ChapterRef] = field(default_factory=list)


class ChapterLocked(Exception):
    """The source says this chapter needs an account we don't have."""


class Provider:
    name = "base"
    #: feed GUIDs are f"{guid_prefix}-{source_id}"; must never change once
    #: episodes are published, or podcast apps re-download everything
    guid_prefix = ""
    raw_ext = ".html"          # extension of the cached per-chapter artifact
    can_series = False
    #: a `session.CookieSession`, if this source has logged-in content
    session = None

    # -- routing ---------------------------------------------------------
    def handles(self, source: str) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def recognizes(self, raw: str) -> bool:
        """Is this saved artifact (e.g. an .html on disk) one of ours? Lets a
        saved page be parsed by the site that produced it."""
        return False

    # -- series ----------------------------------------------------------
    def series(self, source: str, ctx: FetchContext) -> SeriesInfo:  # pragma: no cover
        raise NotImplementedError

    # -- one chapter -----------------------------------------------------
    def check_fetchable(self, ref: ChapterRef, ctx: FetchContext) -> None:
        if not ref.unlocked:
            raise ChapterLocked("chapter is marked locked")

    def fetch(self, ref: ChapterRef, ctx: FetchContext) -> str:  # pragma: no cover
        """The raw artifact for one chapter, from the network or a local slice."""
        raise NotImplementedError

    def parse(self, raw: str, ref: ChapterRef, ctx: FetchContext) -> Document:  # pragma: no cover
        """Raw artifact -> Document. Pure and offline: same input, same output."""
        raise NotImplementedError

    def fetch_cover(self, url: str, ctx: FetchContext) -> bytes | None:
        """Cover image bytes, or None. Providers restrict this to their own
        hosts: cover URLs come from page metadata an author controls."""
        return None

    # -- one-off sources ---------------------------------------------------
    def ref_for(self, source: str) -> ChapterRef:
        return ChapterRef(source_id="", order=0, title="", slug="", url=source)

    def read(self, source: str, ctx: FetchContext) -> Document:
        ref = self.ref_for(source)
        raw = self.fetch(ref, ctx)
        return stamp_provenance(self.parse(raw, ref, ctx), raw)

    @property
    def guid(self) -> str:
        return self.guid_prefix or self.name
