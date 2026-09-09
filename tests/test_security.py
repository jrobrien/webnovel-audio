"""Regression tests for injection / traversal hardening in HTML processing."""
import json
import os.path

from webnovel_audio.royalroad import _safe_id, _safe_slug, fetch_asset, parse_fiction
from webnovel_audio.serve import _h
from webnovel_audio.textout import _yaml


def test_safe_slug_cannot_traverse():
    for raw in ("../../etc/passwd", "x/../../y", "a/b\\c:d", "....//x", "/abs/path",
                "..", ".", "  ", "con\x00x"):
        s = _safe_slug(raw, "fallback")
        assert "/" not in s and "\\" not in s and "\x00" not in s
        assert s and not s.startswith((".", "-"))
        # stays inside the library directory no matter what
        assert os.path.normpath(os.path.join("/lib", s, "cover.jpg")).startswith("/lib/")
    assert _safe_slug("salvage-run-scifi.v2") == "salvage-run-scifi.v2"


def test_safe_id():
    assert _safe_id("424242") == "424242"
    assert _safe_id("../7") == "" and _safe_id("07x") == "" and _safe_id(None) == ""


def test_parse_fiction_sanitises_crafted_chapter_list():
    chapters = [
        {"id": "../../evil", "slug": "x", "title": "bad id", "order": 0},
        {"id": "555", "slug": "../../../etc/shadow", "title": "ok id, bad slug", "order": 1},
    ]
    html = (
        '<html><head><meta property="og:url" '
        'content="https://www.royalroad.com/fiction/9/demo"></head><body>'
        f"<script>window.chapters = {json.dumps(chapters)};</script></body></html>"
    )
    fi = parse_fiction(html, url="https://www.royalroad.com/fiction/9/demo")
    assert len(fi.chapters) == 1                      # the non-numeric id is dropped
    c = fi.chapters[0]
    assert c.rr_id == "555"
    assert "/" not in c.slug and ".." not in c.slug
    assert "/chapter/555/" in c.url and "shadow" in c.url and "../" not in c.url


def test_fetch_asset_rejects_non_royalroad_host():
    # host check happens before any network call
    assert fetch_asset("http://169.254.169.254/latest/meta-data/") is None
    assert fetch_asset("https://evil.example/x.jpg") is None
    assert fetch_asset("not a url") is None


def test_serve_html_escape_covers_quotes_and_angles():
    out = _h('x"><script>alert(1)</script>')
    assert '"' not in out and "<" not in out and ">" not in out
    assert "&quot;" in out and "&lt;script&gt;" in out


def test_yaml_strips_control_chars():
    assert _yaml("line1\nline2\ttab") == '"line1 line2 tab"'
    assert "\n" not in _yaml("a\r\nb")
