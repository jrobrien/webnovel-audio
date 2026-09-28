"""ScribbleHub provider, against synthetic pages that mimic the site's markup
(paged TOC, newest first, `order` attribute, #chp_raw body)."""
import os

import pytest

from webnovel_audio import providers, sync
from webnovel_audio.config import Config
from webnovel_audio.db import DB
from webnovel_audio.providers import ChapterRef
from webnovel_audio.providers import scribblehub as sh

SERIES = "https://www.scribblehub.com/series/5150/iron-choir/"


def _toc_items(orders_ids):
    out = []
    for order, cid in orders_ids:
        title = "Glossary" if order == 1 and cid == 9999 else f"Chapter {order} – Part {order}"
        out.append(
            f'<li class="toc_w" order="{order}"><a class="toc_a" '
            f'href="https://www.scribblehub.com/read/5150-iron-choir/chapter/{cid}/">{title}</a>'
            f'<span class="fic_date_pub" title="Sep 24, 2026 09:06 AM">Sep 24, 2026</span></li>')
    return "".join(out)


def _page(entries, total, pages):
    """One TOC page (the series page when it's the first)."""
    return f"""<html><head>
<meta property="og:image" content="https://cdn.scribblehub.com/images/5/iron-choir.jpg">
<meta property="og:url" content="{SERIES}">
<meta property="og:title" content="Iron Choir">
</head><body><div class="fic_title">Iron Choir</div>
<span class="auth_name_fic">Quill</span>
<a class="fic_genre" href="#">Fantasy</a><a class="stag" href="#">Crafting</a>
<span>Ongoing - Updated</span>
<script>$('#pagination-mesh-toc').pagination({{ items: {pages}, itemsOnPage: 1 }});</script>
<ol class="toc_ol">{_toc_items(entries)}</ol>
<input type="hidden" id="chpcounter" value="{total}">
<script type="application/ld+json">{{"ratingValue":"4.6"}}</script>
</body></html>"""


def _site(n, per=3, glossary_first=False):
    """{url: html} for a series of n chapters, `per` per TOC page, newest first.
    Chapter ids are 100.. in creation order; a glossary created last (id 9999)
    can be placed at order 1, shifting everything else down."""
    chapters = [(i + 1, 100 + i) for i in range(n)]
    if glossary_first:
        chapters = [(1, 9999)] + [(o + 1, cid) for o, cid in chapters]
    total = len(chapters)
    newest = sorted(chapters, reverse=True)
    pages = [newest[i:i + per] for i in range(0, total, per)]
    site = {SERIES: _page(pages[0], total, len(pages))}
    for k, entries in enumerate(pages[1:], 2):
        site[f"{SERIES}?toc={k}"] = _page(entries, total, len(pages))
    return site


class _Net:
    def __init__(self, site):
        self.site, self.calls = site, []

    def __call__(self, url, ctx, *, referer=""):
        self.calls.append((url, referer))
        if "/chapter/" in url:
            return CHAPTER
        return self.site[url]

    def install(self, monkeypatch):
        monkeypatch.setattr(sh.ScribbleHubProvider, "_get",
                            lambda _self, url, ctx, referer="": self(url, ctx, referer=referer))
        return self


CHAPTER = f"""<html><head><title>Iron Choir - Chapter 1 | Scribble Hub</title>
<link rel="canonical" href="https://www.scribblehub.com/read/5150-iron-choir/chapter/100/">
</head><body>
<div class="chapter-title">Chapter 1 – Part 1</div>
<div class="chp_byauthor"><a href="{SERIES}">Iron Choir</a> by
<a href="https://www.scribblehub.com/profile/1/quill/">Quill</a></div>
<div id="chp_contents"><div class="prenext">
<a class="btn-wi btn-next" href="https://www.scribblehub.com/read/5150-iron-choir/chapter/101/">Next</a>
</div><div class="chp_raw" id="chp_raw">
<p>The forge sang all night.<sup class="modern-footnotes-footnote">1</sup></p>
<p><em>Too loud,</em> Mara thought.</p>
<table><tr><td>Skill: Runic Eye</td></tr></table>
<div class="wi_authornotes">Thanks for reading!</div>
<div> <div class="tbl_of"><table><tr>
<td>Name : </td>
<td>Mara</td>
</tr></table></div>
<blockquote><p>Hey there, it's me the author.</p></blockquote></div>
</div></div></body></html>"""


def _ctx(known=()):
    ctx = providers.context(Config(), providers.get("scribblehub"))
    ctx.known = list(known)
    return ctx


def test_routing_and_series_url():
    assert providers.resolve(SERIES).name == "scribblehub"
    ch = "https://www.scribblehub.com/read/5150-iron-choir/chapter/100/"
    assert providers.resolve_series(ch).name == "scribblehub"
    assert sh.series_url(ch) == ("5150", "iron-choir", SERIES)
    assert providers.for_raw(CHAPTER).name == "scribblehub"


def test_toc_page_is_zero_based_and_id_independent():
    refs = sh.parse_toc_page(_site(4, glossary_first=True)[SERIES])
    assert [(r.order, r.source_id) for r in refs] == [(4, "103"), (3, "102"), (2, "101")]
    assert refs[0].published_at == "2026-09-24T09:06:00"
    assert refs[0].slug == "chapter-5-part-5"


def test_full_walk_then_incremental(monkeypatch):
    prov = providers.get("scribblehub")
    net = _Net(_site(7)).install(monkeypatch)

    fi = prov.series(SERIES, _ctx())
    assert [c.source_id for c in fi.chapters] == [str(100 + i) for i in range(7)]
    assert [c.order for c in fi.chapters] == list(range(7))
    assert len(net.calls) == 3                     # 7 chapters, 3 per page
    # later pages carry the series page as Referer, or Cloudflare says 403
    assert all(ref == SERIES for url, ref in net.calls if "?toc=" in url)
    assert fi.title == "Iron Choir" and fi.author == "Quill" and fi.rating == 4.6
    assert fi.status == "ONGOING" and "Fantasy" in fi.tags

    # two new chapters: only the first page is needed
    net2 = _Net(_site(9)).install(monkeypatch)
    fi2 = prov.series(SERIES, _ctx(fi.chapters))
    assert len(net2.calls) == 1
    assert [c.source_id for c in fi2.chapters][-2:] == ["107", "108"]
    assert [c.order for c in fi2.chapters] == list(range(9))


def test_renumbering_forces_a_full_walk(monkeypatch):
    """A glossary inserted at order 1 shifts every chapter; the known list is
    stale and must not be trusted."""
    prov = providers.get("scribblehub")
    _Net(_site(7)).install(monkeypatch)
    old = prov.series(SERIES, _ctx()).chapters

    net = _Net(_site(7, glossary_first=True)).install(monkeypatch)
    fi = prov.series(SERIES, _ctx(old))
    assert len(net.calls) == 3
    assert fi.chapters[0].source_id == "9999" and fi.chapters[0].order == 0
    assert fi.chapters[1].source_id == "100" and fi.chapters[1].order == 1


def test_parse_chapter():
    prov = providers.get("scribblehub")
    ref = ChapterRef(source_id="100", order=0, title="", slug="",
                     url="https://www.scribblehub.com/read/5150-iron-choir/chapter/100/")
    doc = prov.parse(CHAPTER, ref, _ctx())
    assert doc.chapter_title == "Chapter 1 – Part 1"
    assert doc.fiction_title == "Iron Choir" and doc.author == "Quill"
    assert doc.next_url.endswith("/chapter/101/")
    texts = [b.text for b in doc.blocks]
    assert texts[0] == "The forge sang all night."          # footnote marker dropped
    assert doc.blocks[1].italic                               # thought italics kept
    assert doc.blocks[2].kind == "system"
    assert not any("Thanks for reading" in t for t in texts)  # author note dropped
    # the editor's wrapper divs must not glue a table and what follows into one block
    assert [b.kind for b in doc.blocks[3:]] == ["system", "system"]
    assert doc.blocks[3].text == "Name : Mara"
    assert doc.blocks[4].text == "Hey there, it's me the author."


def test_add_and_render_offline(tmp_path, monkeypatch):
    _Net(_site(4)).install(monkeypatch)
    monkeypatch.setattr(sync, "_cache_cover", lambda *a, **k: None)
    cfg = Config()
    cfg.library.state_db = str(tmp_path / "s.db")
    cfg.library.library_dir = str(tmp_path / "lib")

    info = sync.add_series(cfg, SERIES, start="start", log=lambda *_: None)
    assert info["provider"] == "scribblehub" and info["chapters"] == 4
    res = sync.run_stage(cfg, "rendered", "iron-choir", lo=1, hi=1, backend="null",
                         log=lambda *_: None)
    assert res.rendered == 1 and res.errors == 0

    db = DB(cfg.library.state_db)
    s = db.get_series("iron-choir")
    c = db.chapters(s["id"])[0]
    assert c["status"] == "rendered" and c["raw_path"].endswith("/.raw/100.html")
    md = open(c["text_path"], encoding="utf-8").read()
    assert 'provider: "scribblehub"' in md and 'source_id: "100"' in md
    assert "The forge sang all night." in md
    db.close()


def test_same_id_on_two_sites_are_two_series(tmp_path):
    from webnovel_audio.providers import SeriesInfo

    db = DB(str(tmp_path / "s.db"))
    a = db.upsert_series(SeriesInfo(provider="royalroad", source_id="5150", slug="a", title="A"))
    b = db.upsert_series(SeriesInfo(provider="scribblehub", source_id="5150", slug="b", title="B"))
    assert a != b and len(db.list_series()) == 2
    db.close()


@pytest.mark.parametrize("text,want", [
    ("Sep 24, 2026 09:06 AM", "2026-09-24T09:06:00"),
    ("nonsense", ""),
])
def test_published(text, want):
    assert sh._published(text) == want


def test_published_relative():
    import datetime as dt
    now = dt.datetime(2026, 9, 27, 12, 0, 0)
    assert sh._published("17 hours ago", now) == "2026-09-26T19:00:00"


def test_single_artifact_provider_contract(tmp_path, monkeypatch):
    """The interface must fit a source that is one file chopped into chapters
    (e.g. a Project Gutenberg .txt): chapters without URLs, `fetch` slicing a
    locally cached whole, and provider state that survives between runs."""
    from webnovel_audio.document import Document
    from webnovel_audio.normalize import Block
    from webnovel_audio.providers import Provider, SeriesInfo

    BOOK = "CHAPTER I\nIt was a truth.\nCHAPTER II\nIt was a lie.\n"

    class Book(Provider):
        name, can_series, raw_ext = "testbook", True, ".txt"
        downloads = 0

        def handles(self, source):
            return source.startswith("book://")

        def _whole(self, ctx):
            path = os.path.join(ctx.raw_dir, "_book.txt") if ctx.raw_dir else ""
            if path and os.path.exists(path):
                return open(path).read()
            Book.downloads += 1
            if path:
                os.makedirs(ctx.raw_dir, exist_ok=True)
                open(path, "w").write(BOOK)
            return BOOK

        def series(self, source, ctx):
            parts = self._whole(ctx).split("CHAPTER ")[1:]
            chs = [ChapterRef(source_id=f"ch-{i + 1:03d}", order=i, title=f"Chapter {i + 1}",
                              slug=f"chapter-{i + 1}") for i in range(len(parts))]
            return SeriesInfo(source_id="42", slug="a-book", title="A Book", url=source,
                              chapters=chs, state={"splitter": "CHAPTER", "seen": len(chs)})

        def fetch(self, ref, ctx):
            assert ctx.state == {"splitter": "CHAPTER", "seen": 2}
            parts = self._whole(ctx).split(ctx.state["splitter"] + " ")[1:]
            return parts[ref.order]

        def parse(self, raw, ref, ctx):
            body = raw.split("\n", 1)[1].strip()
            return Document(blocks=[Block("paragraph", body)], chapter_title=ref.title)

    monkeypatch.setattr(providers, "PROVIDERS", [Book(), *providers.PROVIDERS])
    cfg = Config()
    cfg.library.state_db = str(tmp_path / "s.db")
    cfg.library.library_dir = str(tmp_path / "lib")

    sync.add_series(cfg, "book://42", start="start", log=lambda *_: None)
    res = sync.run_stage(cfg, "parsed", "a-book", log=lambda *_: None)
    assert res.rendered == 2 and res.errors == 0
    db = DB(cfg.library.state_db)
    s = db.get_series("a-book")
    assert db.provider_state(s) == {"splitter": "CHAPTER", "seen": 2}
    rows = db.chapters(s["id"])
    assert [r["url"] for r in rows] == ["", ""]
    assert "It was a lie." in open(rows[1]["text_path"], encoding="utf-8").read()
    # one download at add time (no bundle yet), one into the raw cache after
    assert Book.downloads == 2
    db.close()
