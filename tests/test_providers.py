import os

from webnovel_audio import providers
from webnovel_audio.config import Config

_CH = os.path.join(os.path.dirname(__file__), "..", "samples", "salvage-run-ch1.html")


def test_resolve_routing():
    assert isinstance(providers.resolve("https://www.royalroad.com/fiction/1/x/chapter/2/y"),
                      providers.RoyalRoadProvider)
    assert isinstance(providers.resolve("/tmp/whatever.html"), providers.LocalHtmlProvider)
    assert providers.resolve("chapter.txt") is None            # plain text -> not a provider
    assert providers.resolve("notes.md") is None

    assert providers.resolve_series("https://www.royalroad.com/fiction/1/x") is not None
    assert providers.resolve_series("/tmp/x.html") is None     # LocalHtml is chapter-only


def test_local_html_provider_stamps_provenance():
    if not os.path.exists(_CH):
        return
    doc = providers.LocalHtmlProvider().read(_CH, cfg=Config())
    assert doc.chapter_title == "1. Dead Air"
    assert len(doc.raw_sha256) == 64 and doc.raw_bytes > 0
    assert doc.retrieved_at                                    # from the file's mtime
    assert any(b.kind == "chat" for b in doc.blocks)


def test_stamp_provenance_is_deterministic():
    from webnovel_audio.ingest import Document

    a = providers.stamp_provenance(Document(blocks=[]), "hello world")
    b = providers.stamp_provenance(Document(blocks=[]), "hello world")
    assert a.raw_sha256 == b.raw_sha256 and a.raw_bytes == 11
