import os

from webnovel_audio.providers.royalroad import _extract_js_array, parse_fiction

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "samples", "salvage-run-fiction.html")


def test_extract_js_array_handles_brackets_in_strings():
    html = 'x window.chapters = [{"title":"Ch [1] end","slug":"a"},{"title":"b"}]; more'
    got = _extract_js_array(html, "chapters")
    assert got == '[{"title":"Ch [1] end","slug":"a"},{"title":"b"}]'


def test_parse_fiction_fixture():
    if not os.path.exists(FIXTURE):
        return
    fi = parse_fiction(open(FIXTURE, encoding="utf-8").read(),
                       url="https://www.royalroad.com/fiction/424242/salvage-run")
    assert fi.source_id == "424242"
    assert fi.slug == "salvage-run"
    assert fi.title == "Salvage Run"
    assert fi.author == "voidwright"
    assert len(fi.chapters) == 6
    first, last = fi.chapters[0], fi.chapters[-1]
    assert first.order == 0 and last.order == 5
    assert first.source_id.isdigit()
    assert "/chapter/1001/" in first.url and first.url.startswith("https://")
    assert first.published_at.startswith("2025-")
    assert all(c.unlocked for c in fi.chapters)
    # a "]" inside a JSON string must not truncate window.chapters
    assert last.title == "6. Salvage Rights [it ends]"


def test_request_delay_survives_client_churn(monkeypatch):
    """A provider builds a fresh client per chapter, so the politeness gap has
    to live on the class, per host — otherwise `fetch <slug> 1-50` fires 50
    requests back to back."""
    import time

    import httpx

    from webnovel_audio.providers.http import PoliteClient

    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    monkeypatch.setattr(httpx.Client, "get", lambda self, url, **kw: _FakeResp())
    monkeypatch.setattr(PoliteClient, "_last_request", {})

    for _ in range(3):
        c = PoliteClient(delay=2.5)
        c.get("https://www.royalroad.com/x")
        c.close()

    # first request is free, the next two each wait out most of the gap
    assert len(slept) == 2, slept
    assert all(2.0 < s <= 2.5 for s in slept), slept

    # a different host has its own gap
    c = PoliteClient(delay=2.5)
    c.get("https://www.scribblehub.com/x")
    c.close()
    assert len(slept) == 2, slept


class _FakeResp:
    status_code = 200
    text = "<html></html>"
    content = b"<html></html>"
    headers: dict = {}
    url = "https://www.royalroad.com/x"

    def raise_for_status(self):
        return None


def _fiction_fixture():
    import os.path
    p = os.path.join(os.path.dirname(__file__), "..", "samples", "salvage-run-fiction.html")
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def test_parse_fiction_metadata():
    html = _fiction_fixture()
    if not html:
        return
    fi = parse_fiction(html, url="https://www.royalroad.com/fiction/424242/salvage-run")
    assert fi.status == "ONGOING"                 # "Original" must not win
    assert fi.rating == 4.51
    assert "Sci-fi" in fi.tags and "Space Opera" in fi.tags
    assert fi.warnings == ["Graphic Violence", "Profanity"]


def test_metadata_extractors_degrade_to_empty():
    """Royal Road can rename any of these presentational classes; losing a tag
    list must never break a sync, and empty is a truthful 'we don't know'."""
    fi = parse_fiction("<html><body><h1>Bare</h1></body></html>", url="https://rr/fiction/1/x")
    assert fi.tags == [] and fi.warnings == [] and fi.status == "" and fi.rating == 0.0

    # malformed rating / weird label casing must not raise either
    html = ('<html><head><meta property="books:rating:value" content="not-a-number">'
            '</head><body><span class="label">ongoing</span></body></html>')
    fi = parse_fiction(html, url="https://rr/fiction/1/x")
    assert fi.rating == 0.0 and fi.status == "ONGOING"


def test_no_synopsis_is_captured():
    """We deliberately don't store the author's description — the source URL
    stands in for it. Keeps their prose out of our DB and feed."""
    from webnovel_audio.providers import SeriesInfo

    assert not hasattr(SeriesInfo("1", "s", "t"), "description")


def test_saved_page_is_recognised():
    from webnovel_audio.providers import for_raw

    p = os.path.join(os.path.dirname(__file__), "..", "samples", "salvage-run-ch1.html")
    if not os.path.exists(p):
        return
    assert for_raw(open(p, encoding="utf-8").read()).name == "royalroad"
    assert for_raw("<html><body><p>hi</p></body></html>") is None


def test_tags_are_read_from_a_browser_saved_series_page():
    # the saved page has no `a.fiction-tag`; its tags are links to the tag search
    from webnovel_audio.providers.royalroad import RoyalRoadProvider
    html = ('<html><head><meta property="og:url" content="https://www.royalroad.com/fiction/1/x">'
            '<meta property="og:title" content="X"></head><body><h1>X</h1>'
            '<a href="https://www.royalroad.com/fictions/search?tagsAdd=litrpg" class="p">LitRPG</a>'
            '<a href="https://www.royalroad.com/fictions/search?tagsAdd=female_lead">Female Lead</a>'
            '</body></html>')
    from bs4 import BeautifulSoup
    from webnovel_audio.providers import royalroad
    assert royalroad._parse_tags(BeautifulSoup(html, "lxml")) == ["LitRPG", "Female Lead"]


# The redesigned layout, as served to a signed-in browser: the reader's own
# "My Profile" link (in the account dropdown) comes before the author card.
_SIGNED_IN_PAGE = """<html><head><meta property="og:title" content="7. Seven - Demo"></head><body>
<header><div data-rr-dropdown-content role="menu">
  <a href="/profile/111">My Profile</a><a href="/profile/111/fictions">Fictions</a></div></header>
<div class="chapter-inner chapter-content"><p>Text.</p></div>
<div class="card"><a href="/profile/222"><img src="a.png"></a>
  <div><a href="/profile/222"><h4>Real Author</h4></a>
  <div data-rr-dropdown><button><span><i class="fas fa-hands-holding-dollar mr-1"></i>Donate</span></button>
    <div data-rr-dropdown-content role="menu">
      <a href="https://patreon.com/RealA?utm_medium=unknown&utm_source=join_link">Patreon</a>
      <a href="https://paypal.me/RealA">PayPal</a>
      <a href="javascript:alert(1)">bad</a></div></div></div>
  <a href="/profile/222/fictions">3 Fictions</a></div>
<div class="author-note"><a href="https://www.patreon.com/posts/teaser-1">teaser</a></div>
</body></html>"""


def test_author_is_the_card_not_the_signed_in_readers_menu_link():
    from webnovel_audio.providers.royalroad import parse_chapter
    doc = parse_chapter(_SIGNED_IN_PAGE, url="https://www.royalroad.com/fiction/9/d/chapter/7/s")
    assert doc.author == "Real Author"
    assert doc.meta["author_url"] == "https://www.royalroad.com/profile/222"
    # the Donate menu only, tracking parameters dropped, no non-web links, and
    # not a Patreon *post* linked from an author's note
    assert doc.meta["support_urls"] == ["https://patreon.com/RealA",
                                        "https://paypal.me/RealA"]
    fi = parse_fiction(_SIGNED_IN_PAGE, url="https://www.royalroad.com/fiction/9/d")
    assert fi.author == "Real Author" and fi.support_urls == doc.meta["support_urls"]


def test_old_layout_author_still_found():
    html = ('<html><body><a href="/profile/1">Someone in a sidebar</a>'
            '<div class="fic-title"><h4><span>by </span><a href="/profile/7">Old Author</a>'
            '</h4></div></body></html>')
    fi = parse_fiction(html, url="https://www.royalroad.com/fiction/9/d")
    assert fi.author == "Old Author" and fi.support_urls == []
