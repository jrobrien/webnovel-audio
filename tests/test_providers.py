import os

from bs4 import BeautifulSoup

from webnovel_audio import providers
from webnovel_audio.config import Config
from webnovel_audio.providers.html import blocks_from_container, system_clause_text

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
    prov = providers.LocalHtmlProvider()
    doc = prov.read(_CH, providers.context(Config(), prov))
    assert doc.chapter_title == "1. Dead Air"
    assert len(doc.raw_sha256) == 64 and doc.raw_bytes > 0
    assert doc.retrieved_at                                    # from the file's mtime
    assert any(b.kind == "chat" for b in doc.blocks)


def test_stamp_provenance_is_deterministic():
    from webnovel_audio.document import Document, stamp_provenance

    a = stamp_provenance(Document(blocks=[]), "hello world")
    b = stamp_provenance(Document(blocks=[]), "hello world")
    assert a.raw_sha256 == b.raw_sha256 and a.raw_bytes == 11


def test_system_clause_text_breaks_stat_lines_not_headers():
    # one stat per <br> line -> each becomes its own spoken clause, but a bare
    # header with no value of its own ("Attributes:") stays glued to what follows
    raw = "Level: 7\nArchetype: Watcher\n\nAttributes:\nStrength: 15.5\nSpeed: 19.25"
    assert system_clause_text(raw) == (
        "Level: 7. Archetype: Watcher. Attributes: Strength: 15.5. Speed: 19.25"
    )


def test_stat_box_br_lines_become_separate_system_clauses():
    # a real RoyalRoad LitRPG stat box: one <p><strong><em> with <br> per stat,
    # no actual <table> -- the <br> structure used to be flattened to bare spaces
    html = (
        "<div>"
        "<p><strong><em>Level: 7<br>Archetype: Watcher<br><br>"
        "Attributes:<br>Strength: 15.5<br>Speed: 19.25</em></strong></p>"
        "</div>"
    )
    container = BeautifulSoup(html, "lxml").div
    blocks = blocks_from_container(container)
    assert len(blocks) == 1 and blocks[0].kind == "system"
    assert blocks[0].text == (
        "Level: 7. Archetype: Watcher. Attributes: Strength: 15.5. Speed: 19.25"
    )


def test_stat_box_lines_are_system_not_chat_and_headers_are_titled():
    # revolver-chronicles #12: every `[Label: value ->]` line was read by its own
    # chat voice, a trailing arrow became "to", and `<<ATTRIBUTES>>` reached the
    # narrator with its brackets
    html = (
        "<div>"
        "<p>[Karmic Level 1 -&gt; ]</p>"
        "<p>&lt;&lt;ATTRIBUTES&gt;&gt;</p>"
        "<p>[Ambition: 15 -&gt; ]</p>"
        "<p>[Attunement: 9 -&gt; ]</p>"
        "<p>[HP: 577 -&gt; ]</p>"
        "</div>"
    )
    blocks = blocks_from_container(BeautifulSoup(html, "lxml").div)
    assert [b.kind for b in blocks] == ["system"] * 5
    assert [b.text for b in blocks] == [
        "Karmic Level 1 ->", "Attributes.", "Ambition: 15 ->", "Attunement: 9 ->",
        "HP: 577 ->"]

    from webnovel_audio.config import Config
    from webnovel_audio.segment import build_segments
    segs = build_segments(blocks, Config())
    assert {s.style for s in segs} == {"system"} and len({s.voice for s in segs}) == 1
    assert [s.text for s in segs] == [
        "Karmic Level one", "Attributes.", "Ambition: fifteen", "Attunement: nine",
        "hit points: five hundred seventy-seven"]
