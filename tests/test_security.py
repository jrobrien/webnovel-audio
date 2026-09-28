"""Regression tests for injection / traversal hardening in HTML processing."""
import json
import pytest
import os.path

from webnovel_audio.providers.http import fetch_asset
from webnovel_audio.providers.royalroad import ASSET_HOSTS, _safe_id, parse_fiction
from webnovel_audio.safepath import safe_slug as _safe_slug
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
    assert c.source_id == "555"
    assert "/" not in c.slug and ".." not in c.slug
    assert "/chapter/555/" in c.url and "shadow" in c.url and "../" not in c.url


def test_fetch_asset_rejects_other_hosts():
    # host check happens before any network call
    assert fetch_asset("http://169.254.169.254/latest/meta-data/", ASSET_HOSTS) is None
    assert fetch_asset("https://evil.example/x.jpg", ASSET_HOSTS) is None
    assert fetch_asset("https://evilroyalroad.com/x.jpg", ASSET_HOSTS) is None
    assert fetch_asset("not a url", ASSET_HOSTS) is None


def test_serve_html_escape_covers_quotes_and_angles():
    out = _h('x"><script>alert(1)</script>')
    assert '"' not in out and "<" not in out and ">" not in out
    assert "&quot;" in out and "&lt;script&gt;" in out


def test_yaml_strips_control_chars():
    assert _yaml("line1\nline2\ttab") == '"line1 line2 tab"'
    assert "\n" not in _yaml("a\r\nb")


def test_login_does_not_verify(tmp_path, monkeypatch, capsys):
    """0.2.1: `login` stores cookies and says so — it never claims to have
    verified them (that needed an account page whose markup drifts)."""
    import types

    from webnovel_audio import cli
    from webnovel_audio.providers import session

    monkeypatch.setattr(session, "CONFIG_DIR", str(tmp_path))

    rc = cli._cmd_login(types.SimpleNamespace(
        provider=None, logout=False, status=False, cookies_file=None,
        cookie="__cfduid=abc; .AspNetCore.Identity.Application=xyz"))
    out = capsys.readouterr().out
    assert rc == 0 and "saved 2 cookie(s)" in out
    assert "authenticated" not in out.lower()
    assert (tmp_path / "session.json").exists()

    rc = cli._cmd_login(types.SimpleNamespace(
        provider=None, logout=False, status=True, cookies_file=None, cookie=None))
    out = capsys.readouterr().out
    assert rc == 0 and "not verified" in out


def test_locked_chapter_points_at_login(tmp_path, monkeypatch):
    """A locked chapter fails with an actionable message rather than silently
    caching a paywall page."""
    from webnovel_audio import providers, sync
    from webnovel_audio.config import Config
    from webnovel_audio.db import DB
    from webnovel_audio.providers import ChapterRef, SeriesInfo, session

    monkeypatch.setattr(session, "CONFIG_DIR", str(tmp_path))
    cfg = Config()
    cfg.library.state_db = str(tmp_path / "s.db")
    cfg.library.library_dir = str(tmp_path / "lib")
    db = DB(cfg.library.state_db)
    sid = db.upsert_series(SeriesInfo(provider="royalroad", source_id="9", slug="demo",
                                      title="Demo", url="https://rr/9"))
    db.replace_chapters(sid, [ChapterRef(source_id="1", order=0, title="Locked",
                                         slug="locked", url="https://rr/c/1",
                                         published_at="", unlocked=False)])
    c = db.chapters(sid)[0]

    prov = providers.get("royalroad")
    monkeypatch.setattr(type(prov), "_get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not fetch a locked chapter")))
    ctx = providers.context(cfg, prov)
    with pytest.raises(sync.ChapterLocked) as exc:
        sync._do_fetch(cfg, db, prov, ctx, str(tmp_path / "demo"), c)
    assert "login" in str(exc.value)
    db.close()
