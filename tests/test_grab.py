"""The fetch-ingest-render loop, with the browser played by an HTTP client."""
import http.client
import json
import os
import threading
import time

import pytest

from webnovel_audio import providers, sync
from webnovel_audio.config import Config
from webnovel_audio.db import DB
from webnovel_audio.grab import PROTOCOL, GrabLoop, PageCatcher

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "samples", "salvage-run-fiction.html")
pytestmark = pytest.mark.skipif(not os.path.exists(FIXTURE), reason="fixture missing")


def _page(chapter, body='<div class="chapter-content"><p>Hello there.</p></div>'):
    return ('<html><head><meta property="og:url" content="https://www.royalroad.com/fiction/'
            f'424242/salvage-run/chapter/{chapter}/x"></head><body>{body}</body></html>')


@pytest.fixture
def series(tmp_path, monkeypatch):
    fiction = open(FIXTURE, encoding="utf-8").read()
    monkeypatch.setattr(providers.RoyalRoadProvider, "_get", lambda self, url, ctx: fiction)
    monkeypatch.setattr(sync, "_cache_cover", lambda *a, **k: None)
    cfg = Config()
    cfg.library.state_db = str(tmp_path / "state.db")
    cfg.library.library_dir = str(tmp_path / "lib")
    sync.add_series(cfg, "https://www.royalroad.com/fiction/424242/salvage-run",
                    start="start", log=lambda *_: None)
    db = DB(cfg.library.state_db)
    ids = [c["source_id"] for c in db.chapters(db.get_series("salvage-run")["id"])]
    db.close()
    return cfg, ids


class Browser:
    """What the extension does: send pages, ask what next."""
    def __init__(self, loop, **headers):
        self.loop, self.headers = loop, headers

    def call(self, method, path, body=None, **over):
        h = {"X-Grab-Token": self.loop.token, "Host": f"127.0.0.1:{self.loop.port}",
             "X-Grab-Protocol": str(PROTOCOL),
             "Content-Type": "application/json", **self.headers, **over}
        h = {k: v for k, v in h.items() if v is not None}
        c = http.client.HTTPConnection("127.0.0.1", self.loop.port, timeout=10)
        c.putrequest(method, path, skip_host=True)
        for k, v in h.items():
            c.putheader(k, v)
        data = json.dumps(body).encode() if body is not None else b""
        c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        out = json.loads(r.read() or b"{}")
        c.close()
        return r.status, out


def _series_page():
    return open(FIXTURE, encoding="utf-8").read()


def _open_series(b):
    """What the extension does first: load the series page the server names, send it."""
    code, nxt = b.call("GET", "/next")
    assert nxt["action"] == "fetch" and nxt["kind"] == "series"
    code, got = b.call("POST", "/page", {"html": _series_page()})
    assert got["ok"] and got["kind"] == "series"


def _start(loop):
    t = threading.Thread(target=loop.run, daemon=True)
    t.start()
    for _ in range(100):
        if loop.port:
            return t
        time.sleep(0.02)
    raise AssertionError("server did not start")


def _fake_render(cfg, delay=0.3):
    def render(series_id, number):
        time.sleep(delay)
        db = DB(cfg.library.state_db)
        row = db.con.execute("SELECT id FROM chapters WHERE series_id=? AND ord=?",
                             (series_id, number - 1)).fetchone()
        db.mark(row["id"], "rendered")
        db.close()
        return type("R", (), {"rendered": 1, "errors": 0})()
    return render


def test_loop_fetches_the_next_chapter_only_after_the_render(series):
    cfg, ids = series
    events = []
    loop = GrabLoop(cfg, "salvage-run", 2, emit=events.append, render=_fake_render(cfg))
    t = _start(loop)
    b = Browser(loop)
    _open_series(b)

    code, nxt = b.call("GET", "/next")
    assert code == 200 and nxt["action"] == "fetch" and nxt["number"] == 1
    # asked again before the page arrived (a reload): the same chapter, not the next
    assert b.call("GET", "/next")[1]["number"] == 1
    code, got = b.call("POST", "/page", {"html": _page(ids[0])})
    assert got["ok"] and got["result"] == "ingested"

    # while chapter 1 renders the browser is told to wait, never given chapter 2
    code, nxt = b.call("GET", "/next")
    assert nxt["action"] in ("wait", "fetch")
    deadline = time.time() + 5
    while nxt["action"] == "wait" and time.time() < deadline:
        time.sleep(0.05)
        nxt = b.call("GET", "/next")[1]
    assert nxt["action"] == "fetch" and nxt["number"] == 2

    assert b.call("POST", "/page", {"html": _page(ids[1])})[1]["ok"]
    # everything is fetched, but the last render is still going: not "done" yet
    nxt = b.call("GET", "/next")[1]
    assert nxt["action"] == "wait" and "last render" in nxt["reason"]
    deadline = time.time() + 5
    while time.time() < deadline:
        nxt = b.call("GET", "/next")[1]
        if nxt["action"] == "done":
            break
        time.sleep(0.05)
    assert nxt["action"] == "done" and nxt["reason"] == "every series done"
    t.join(10)
    assert not t.is_alive()

    db = DB(cfg.library.state_db)
    st = [c["status"] for c in db.chapters(db.get_series("salvage-run")["id"])]
    db.close()
    assert st[:3] == ["rendered", "rendered", "new"]
    assert events[-1]["event"] == "done" and events[-1]["done"] == 2


def test_a_page_that_is_not_a_chapter_ends_the_loop_with_the_reason(series):
    cfg, ids = series
    loop = GrabLoop(cfg, "salvage-run", 5, emit=lambda d: None, render=_fake_render(cfg))
    t = _start(loop)
    b = Browser(loop)
    _open_series(b)
    b.call("GET", "/next")
    code, got = b.call("POST", "/page", {"html": _page(ids[0], '<script id="anubis_challenge"></script>')})
    assert not got["ok"] and got["result"] == "rejected"
    assert b.call("GET", "/next")[1]["action"] == "done"
    t.join(10)
    assert "bot-check" in loop.finished


def test_the_browser_can_stop_it(series):
    cfg, ids = series
    loop = GrabLoop(cfg, "salvage-run", 5, emit=lambda d: None, render=_fake_render(cfg))
    t = _start(loop)
    Browser(loop).call("POST", "/stop", {})
    t.join(10)
    assert not t.is_alive() and "stopped from the browser" in loop.finished


def test_silence_ends_it(series):
    cfg, ids = series
    loop = GrabLoop(cfg, "salvage-run", 5, idle_timeout=0.6, emit=lambda d: None)
    t = _start(loop)
    t.join(10)
    assert not t.is_alive() and "went quiet" in loop.finished


@pytest.mark.parametrize("headers, expect", [
    ({"X-Grab-Token": "wrong"}, "token"),
    ({"X-Grab-Token": None}, "token"),
    ({"Origin": "https://www.royalroad.com"}, "origin"),
    ({"Host": "evil.example:80"}, "host"),
])
def test_requests_that_do_not_come_from_the_extension_are_refused(series, headers, expect):
    cfg, ids = series
    loop = GrabLoop(cfg, "salvage-run", 1, emit=lambda d: None, render=_fake_render(cfg))
    t = _start(loop)
    try:
        code, out = Browser(loop).call("GET", "/next", **headers)
        assert code == 403 and expect in out["error"]
        code, out = Browser(loop).call("POST", "/page", {"html": _page(ids[0])}, **headers)
        assert code == 403
    finally:
        loop.finish("test over")
        t.join(10)
    db = DB(cfg.library.state_db)
    assert db.chapters(db.get_series("salvage-run")["id"])[0]["status"] == "new"
    db.close()


def test_an_extension_origin_with_the_token_is_accepted(series):
    cfg, ids = series
    loop = GrabLoop(cfg, "salvage-run", 1, emit=lambda d: None, render=_fake_render(cfg))
    t = _start(loop)
    try:
        code, out = Browser(loop, Origin="chrome-extension://abcdefgh").call("GET", "/next")
        assert code == 200 and out["action"] == "fetch" and out["kind"] == "series"
    finally:
        loop.finish("test over")
        t.join(10)


@pytest.mark.parametrize("theirs, message", [
    (None, "reload the extension"), (str(PROTOCOL - 1), "reload the extension"),
    (str(PROTOCOL + 1), "update webnovel-audio")])
def test_a_mismatched_extension_is_told_which_side_is_old(series, theirs, message):
    cfg, ids = series
    events = []
    loop = GrabLoop(cfg, "salvage-run", 1, emit=events.append, render=_fake_render(cfg))
    t = _start(loop)
    try:
        code, out = Browser(loop).call("GET", "/next", **{"X-Grab-Protocol": theirs})
        assert code == 426 and out["message"] == message and out["server"] == PROTOCOL
        assert Browser(loop).call("POST", "/page", {"html": _page(ids[0])},
                                  **{"X-Grab-Protocol": theirs})[0] == 426
        assert any("extension is" in e.get("message", "") for e in events)
    finally:
        loop.finish("test over")
        t.join(10)
    db = DB(cfg.library.state_db)
    assert db.chapters(db.get_series("salvage-run")["id"])[0]["status"] == "new"
    db.close()


def test_without_the_token_nothing_is_said_about_versions(series):
    cfg, ids = series
    loop = GrabLoop(cfg, "salvage-run", 1, emit=lambda d: None, render=_fake_render(cfg))
    t = _start(loop)
    try:
        code, out = Browser(loop).call("GET", "/next", **{"X-Grab-Token": "wrong",
                                                          "X-Grab-Protocol": "99"})
        assert code == 403 and "server" not in out
    finally:
        loop.finish("test over")
        t.join(10)


def test_one_chapter_renders_do_not_repeat_the_run_banners(series):
    cfg, ids = series
    events = []
    loop = GrabLoop(cfg, "salvage-run", 1, emit=events.append)
    for d in ({"event": "start"}, {"event": "series", "slug": "x"},
              {"event": "chapter_begin", "number": 1}, {"event": "chapter", "number": 1},
              {"event": "done"}):
        loop._render_emit(d)
    assert [e["event"] for e in events] == ["chapter_begin", "chapter"]


def _add_other_series(cfg, slug, provider, enabled):
    from webnovel_audio.providers import SeriesInfo
    db = DB(cfg.library.state_db)
    sid = db.upsert_series(SeriesInfo(provider=provider, source_id=slug, slug=slug,
                                      title=slug.title(), url=f"https://example/{slug}"))
    db.set_enabled(sid, enabled)
    db.close()


def test_scope_works_like_sync(series):
    """No scope: enabled series only, a paused one untouched (its chapter list
    included). A named series is worked on even when paused."""
    cfg, ids = series
    _add_other_series(cfg, "other-rr", "royalroad", enabled=False)
    _add_other_series(cfg, "elsewhere", "scribblehub", enabled=True)

    everything = GrabLoop(cfg)
    assert [s["slug"] for s in everything._pending] == ["salvage-run"]
    assert everything.skipped_other == ["elsewhere"]          # told to use sync

    named = GrabLoop(cfg, "other-rr")                          # paused, but asked for
    assert [s["slug"] for s in named._pending] == ["other-rr"]

    db = DB(cfg.library.state_db)
    db.set_enabled(db.get_series("salvage-run")["id"], False)
    db.close()
    assert GrabLoop(cfg)._pending == []                        # nothing to do at all

    with pytest.raises(SystemExit, match="not read through the browser"):
        GrabLoop(cfg, "elsewhere")
    with pytest.raises(SystemExit, match="no tracked series"):
        GrabLoop(cfg, "nope")


def test_each_series_gets_its_page_then_its_chapters_then_the_next_series(series):
    cfg, ids = series
    _add_other_series(cfg, "second", "royalroad", enabled=True)
    db = DB(cfg.library.state_db)
    from webnovel_audio.providers import ChapterRef
    sid = db.get_series("second")["id"]
    db.replace_chapters(sid, [ChapterRef(source_id="777", order=0, title="S1", slug="s1",
                                         url="https://example/second/1", unlocked=True)])
    db.close()
    loop = GrabLoop(cfg, None, 1, emit=lambda d: None, render=_fake_render(cfg, 0.05))
    t = _start(loop)
    b = Browser(loop)
    try:
        _open_series(b)                                        # salvage-run's page
        nxt = b.call("GET", "/next")[1]
        assert nxt["kind"] == "chapter" and nxt["number"] == 1
        assert b.call("POST", "/page", {"html": _page(ids[0])})[1]["ok"]
        deadline = time.time() + 5                              # limit 1: on to the next series
        while time.time() < deadline:
            nxt = b.call("GET", "/next")[1]
            if nxt["action"] == "fetch":
                break
            time.sleep(0.05)
        assert nxt["action"] == "fetch" and nxt["kind"] == "series"
        assert nxt["url"] == "https://example/second"
    finally:
        loop.finish("test over")
        t.join(10)


def test_a_series_page_resumes_chapters_fetched_but_never_rendered(series):
    """Chapters an earlier run fetched but did not render would otherwise hold the
    loop back forever (they count as unrendered but nothing would render them)."""
    cfg, ids = series
    rendered = []
    def render(series_id, number):
        rendered.append(number)
        db = DB(cfg.library.state_db)
        row = db.con.execute("SELECT id FROM chapters WHERE series_id=? AND ord=?",
                             (series_id, number - 1)).fetchone()
        db.mark(row["id"], "rendered")
        db.close()
    db = DB(cfg.library.state_db)
    sync.ingest_raw(cfg, db, _page(ids[0]))                    # fetched, not rendered
    db.close()
    loop = GrabLoop(cfg, "salvage-run", 1, emit=lambda d: None, render=render)
    t = _start(loop)
    b = Browser(loop)
    try:
        _open_series(b)
        deadline = time.time() + 5
        while 1 not in rendered and time.time() < deadline:
            time.sleep(0.05)
        assert rendered == [1]
        nxt = b.call("GET", "/next")[1]
        assert nxt["action"] == "fetch" and nxt["number"] == 2     # not stuck waiting
    finally:
        loop.finish("test over")
        t.join(10)


def test_the_page_catcher_hands_back_the_one_page(series):
    catcher = PageCatcher(idle_timeout=5)
    result = {}
    def go():
        result["page"] = catcher.run("https://example/x", on_ready=lambda u: result.setdefault("url", u))
    t = threading.Thread(target=go, daemon=True)
    t.start()
    for _ in range(100):
        if catcher.port:
            break
        time.sleep(0.02)
    assert result["url"].endswith(f"#wa-run={catcher.port}:{catcher.token}")
    b = Browser(catcher)
    assert b.call("POST", "/page", {"html": "<p>the page</p>"})[1]["ok"]
    assert b.call("GET", "/next")[1]["action"] == "done"
    t.join(10)
    assert result["page"] == "<p>the page</p>"


def test_the_page_catcher_gives_up_when_the_browser_is_silent():
    catcher = PageCatcher(idle_timeout=0.3)
    assert catcher.run("https://example/x") is None


def test_the_bundled_extension_is_described_and_zipped():
    import io
    import json
    import zipfile

    from webnovel_audio import grab

    d = grab.extension_dir()
    assert d and os.path.exists(os.path.join(d, "manifest.json"))
    assert grab.extension_version() == json.load(open(os.path.join(d, "manifest.json")))["version"]
    assert grab.version_tuple("0.10.2") > grab.version_tuple("0.9.9")
    page = grab.extension_page().decode()
    assert grab.extension_version() in page and d in page and "Update now" in page
    names = zipfile.ZipFile(io.BytesIO(grab.extension_zip())).namelist()
    assert {os.path.basename(n) for n in names} >= {"manifest.json", "content.js", "background.js",
                                                   "options.html", "announce.js"}
    assert not any(os.path.basename(n).startswith(".") for n in names)


def test_the_grab_server_hosts_the_extension_page_for_its_own_host_only(series):
    cfg, ids = series
    loop = GrabLoop(cfg, "salvage-run", 1, emit=lambda d: None, render=_fake_render(cfg))
    t = _start(loop)
    try:
        def raw(path, host):
            c = http.client.HTTPConnection("127.0.0.1", loop.port, timeout=5)
            c.request("GET", path, headers={"Host": host})      # no token
            r = c.getresponse()
            body = r.read()
            c.close()
            return r, body

        mine = f"127.0.0.1:{loop.port}"
        r, body = raw("/extension", mine)
        assert r.status == 200 and b"Page grabber extension" in body   # nothing secret in it
        assert raw("/extension", "evil.example:80")[0].status == 403
        r, body = raw("/extension.zip", mine)
        assert r.status == 200 and r.getheader("Content-Type") == "application/zip"
    finally:
        loop.finish("test over")
        t.join(10)


def test_next_carries_the_bundled_version_and_a_clean_finish_is_marked(series):
    from webnovel_audio import grab

    cfg, ids = series
    loop = GrabLoop(cfg, "salvage-run", 1, emit=lambda d: None, render=_fake_render(cfg, 0.05))
    t = _start(loop)
    b = Browser(loop)
    _open_series(b)
    nxt = b.call("GET", "/next")[1]
    assert nxt["latest"] == grab.extension_version() and "clean" not in nxt
    b.call("POST", "/page", {"html": _page(ids[0])})
    deadline = time.time() + 5
    while time.time() < deadline:
        nxt = b.call("GET", "/next")[1]
        if nxt["action"] == "done":
            break
        time.sleep(0.05)
    assert nxt["action"] == "done" and nxt["clean"] is True
    t.join(10)

    stopped = GrabLoop(cfg, "salvage-run", 1, emit=lambda d: None)
    t = _start(stopped)
    Browser(stopped).call("POST", "/stop", {})
    assert Browser(stopped).call("GET", "/next")[1]["clean"] is False   # a stop does not close the tab
    t.join(10)


def test_a_missing_or_old_extension_is_named_in_the_log(series):
    from webnovel_audio import grab

    cfg, ids = series
    events = []
    loop = GrabLoop(cfg, "salvage-run", 1, emit=events.append, idle_timeout=1.2)
    loop.hint_after = 0.3                                    # not 20 s, for the test
    t = _start(loop)
    t.join(10)                                               # nobody ever answers
    said = [e["message"] for e in events if e.get("message")]
    assert any("Is the page-grabber extension installed" in m and "/extension" in m for m in said)

    events.clear()
    loop = GrabLoop(cfg, "salvage-run", 1, emit=events.append, render=_fake_render(cfg))
    t = _start(loop)
    try:
        b = Browser(loop, **{"X-Grab-Ext": "0.0.1"})
        b.call("GET", "/next")
        b.call("GET", "/next")                               # said once, not each request
    finally:
        loop.finish("test over")
        t.join(10)
    old = [e["message"] for e in events if "is 0.0.1" in e.get("message", "")]
    assert len(old) == 1 and grab.extension_version() in old[0]
