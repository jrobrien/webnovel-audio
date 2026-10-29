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
from webnovel_audio.grab import PROTOCOL, GrabLoop

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


def _start(loop):
    t = threading.Thread(target=loop.run, daemon=True)
    t.start()
    for _ in range(100):
        if loop.port:
            return t
        time.sleep(0.02)
    raise AssertionError("server did not start")


def _fake_render(cfg, delay=0.3):
    def render(number):
        time.sleep(delay)
        db = DB(cfg.library.state_db)
        row = db.con.execute("SELECT id FROM chapters WHERE ord=?", (number - 1,)).fetchone()
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
    deadline = time.time() + 5
    while time.time() < deadline:
        nxt = b.call("GET", "/next")[1]
        if nxt["action"] == "done":
            break
        time.sleep(0.05)
    assert nxt["action"] == "done" and "2 chapter" in nxt["reason"]
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
        assert code == 200 and out["action"] == "fetch"
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
