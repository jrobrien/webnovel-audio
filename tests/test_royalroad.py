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
