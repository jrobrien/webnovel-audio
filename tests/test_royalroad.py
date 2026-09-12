import os

from webnovel_audio.royalroad import (
    _extract_js_array,
    parse_cookie_header,
    parse_fiction,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "samples", "salvage-run-fiction.html")


def test_extract_js_array_handles_brackets_in_strings():
    html = 'x window.chapters = [{"title":"Ch [1] end","slug":"a"},{"title":"b"}]; more'
    got = _extract_js_array(html, "chapters")
    assert got == '[{"title":"Ch [1] end","slug":"a"},{"title":"b"}]'


def test_parse_cookie_header():
    c = parse_cookie_header(" a=1;  b=two=x ; ")
    assert c == {"a": "1", "b": "two=x"}


def test_parse_fiction_fixture():
    if not os.path.exists(FIXTURE):
        return
    fi = parse_fiction(open(FIXTURE, encoding="utf-8").read(),
                       url="https://www.royalroad.com/fiction/424242/salvage-run")
    assert fi.rr_id == "424242"
    assert fi.slug == "salvage-run"
    assert fi.title == "Salvage Run"
    assert fi.author == "voidwright"
    assert len(fi.chapters) == 6
    first, last = fi.chapters[0], fi.chapters[-1]
    assert first.order == 0 and last.order == 5
    assert first.rr_id.isdigit()
    assert "/chapter/1001/" in first.url and first.url.startswith("https://")
    assert first.published_at.startswith("2025-")
    assert all(c.unlocked for c in fi.chapters)
    # a "]" inside a JSON string must not truncate window.chapters
    assert last.title == "6. Salvage Rights [it ends]"


def test_request_delay_survives_client_churn(monkeypatch):
    """`providers.RoyalRoadProvider.raw` builds a fresh RRClient per chapter, so
    the politeness gap has to live on the class — otherwise `fetch <slug> 1-50`
    fires 50 requests back to back."""
    import time

    import httpx

    from webnovel_audio.royalroad import RRClient

    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    monkeypatch.setattr(httpx.Client, "get", lambda self, url, **kw: _FakeResp())
    monkeypatch.setattr(RRClient, "_last_request", 0.0)

    for _ in range(3):
        c = RRClient(delay=2.5)
        c.get("https://www.royalroad.com/x")
        c.close()

    # first request is free, the next two each wait out most of the gap
    assert len(slept) == 2, slept
    assert all(2.0 < s <= 2.5 for s in slept), slept


class _FakeResp:
    status_code = 200
    text = "<html></html>"
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
    from webnovel_audio.royalroad import FictionInfo

    assert not hasattr(FictionInfo("1", "s", "t"), "description")
