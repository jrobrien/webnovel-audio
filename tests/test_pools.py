"""Resource pools (`pools.py`): jobs queue for the synthesizer, the browser, a
site and the library instead of refusing each other.

The multi-process tests hand-shake over each child's stdout (one JSON event per
line) rather than sleeping: a child says `held` once it has the pool, and lets
go when the parent writes a line to its stdin."""
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

import pytest

from webnovel_audio import pools, sync
from webnovel_audio.config import Config
from webnovel_audio.db import DB

FICTION = os.path.join(os.path.dirname(__file__), "..", "samples", "salvage-run-fiction.html")
CHAPTER_STUB = """<html><head><meta property="og:title" content="4. Deck Four - Salvage Run">
</head><body><div class="chapter-content">
<p>The corridor ended at a sealed door.</p><p>"Careful," Resk said.</p>
</div></body></html>"""

HOLDER = r"""
import json, sys
from webnovel_audio import pools
from webnovel_audio.config import Config
cfg = Config(); cfg.library.state_db = sys.argv[1]
name, shared, wait = sys.argv[2], sys.argv[3] == "shared", sys.argv[4] == "wait"
pools.COMMAND = "holder-test"
emit = lambda d: print(json.dumps(d), flush=True)
with pools.hold(cfg, name, what="test " + name, shared=shared, wait=wait, emit=emit):
    emit({"event": "held"})
    sys.stdin.readline()
emit({"event": "released"})
"""

#: a render in its own process; every synth hold is reported with timestamps
RENDER = r"""
import contextlib, json, sys, time
from webnovel_audio import pools, sync
from webnovel_audio.config import Config
cfg = Config.load(sys.argv[1])
slug, rng = sys.argv[2], sys.argv[3]
emit = lambda d: print(json.dumps(d), flush=True)
real = pools.hold
@contextlib.contextmanager
def traced(cfg, name, **kw):
    with real(cfg, name, **kw):
        t0 = time.time()
        try:
            yield
        finally:
            if name == "synth":
                emit({"event": "span", "t0": t0, "t1": time.time()})
pools.hold = traced
lo, hi = (int(rng), int(rng)) if rng else (None, None)
with sync.sync_lock(cfg, shared=True, wait=True):
    res = sync.run_stage(cfg, "rendered", slug, lo=lo, hi=hi, backend="null",
                         log=lambda *_: None, emit=emit)
emit({"event": "result", "rendered": res.rendered, "errors": res.errors,
      "elsewhere": res.elsewhere})
"""


class Child:
    """A child process speaking JSON lines; `expect` waits for one event."""

    def __init__(self, *argv):
        self.p = subprocess.Popen([sys.executable, "-c", *argv], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True)
        self.events: list[dict] = []
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            try:
                self._q.put(json.loads(line))
            except ValueError:
                pass
        self._q.put(None)

    def expect(self, event, timeout=20.0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                d = self._q.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError(f"no {event!r} from child; got {self.events}") from None
            if d is None:
                raise AssertionError(f"child ended before {event!r}: {self.events}\n"
                                     f"{self.p.stderr.read()}")
            self.events.append(d)
            if d.get("event") == event:
                return d

    def finish(self, timeout=60.0):
        """Read to the end; return every event."""
        while True:
            try:
                d = self._q.get(timeout=timeout)
            except queue.Empty:
                raise AssertionError("child did not finish") from None
            if d is None:
                break
            self.events.append(d)
        self.p.wait(timeout)
        return self.events

    def release(self):
        self.p.stdin.write("\n")
        self.p.stdin.flush()

    def close(self):
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait(10)


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.library.state_db = str(tmp_path / "state.db")
    return c


def holder(cfg, name, *, shared=False, wait=False):
    return Child(HOLDER, cfg.library.state_db, name,
                 "shared" if shared else "exclusive", "wait" if wait else "nowait")


# -- pools.hold ------------------------------------------------------------------

def test_a_second_holder_waits_then_gets_it_and_the_record_survives(cfg):
    a = holder(cfg, "synth")
    b = None
    try:
        a.expect("held")
        path = pools.lock_path(cfg, "synth")
        recs = pools.read_holders(path)
        assert [r["pid"] for r in recs] == [a.p.pid]
        assert recs[0]["what"] == "test synth" and recs[0]["command"] == "holder-test"

        b = holder(cfg, "synth", wait=True)
        w = b.expect("waiting")
        assert w["pool"] == "synth" and w["holder"]["pid"] == a.p.pid
        # b has opened the file and is blocked on it: a's record is intact
        assert [r["pid"] for r in pools.read_holders(path)] == [a.p.pid]

        a.release()
        got = b.expect("acquired")
        assert got["pool"] == "synth" and got["waited_s"] >= 0
        b.expect("held")
        assert [r["pid"] for r in pools.read_holders(path)] == [b.p.pid]
        b.release()
        b.expect("released")
    finally:
        a.close()
        if b:
            b.close()


def test_no_wait_raises_naming_the_holder_and_is_a_sync_locked(cfg):
    a = holder(cfg, "browser")
    try:
        a.expect("held")
        with pytest.raises(sync.SyncLocked) as e:
            with pools.hold(cfg, "browser", wait=False):
                pass
        assert isinstance(e.value, pools.PoolBusy)
        assert e.value.pool == "browser" and e.value.holder["pid"] == a.p.pid
        assert "held by test browser" in str(e.value)
    finally:
        a.close()


def test_a_killed_holder_releases_the_pool(cfg):
    a = holder(cfg, "synth")
    try:
        a.expect("held")
        a.p.send_signal(signal.SIGKILL)
        a.p.wait(10)
        with pools.hold(cfg, "synth", wait=False) as rec:
            assert rec["pid"] == os.getpid()
        # the dead holder's record is not reported as a holder
        assert pools.read_holders(pools.lock_path(cfg, "synth")) == []
    finally:
        a.close()


def test_ctrl_c_while_waiting_interrupts(cfg):
    with pools.hold(cfg, "synth", what="parent"):
        b = holder(cfg, "synth", wait=True)
        try:
            b.expect("waiting")
            b.p.send_signal(signal.SIGINT)
            b.p.wait(10)
            assert b.p.returncode != 0
            assert "KeyboardInterrupt" in b.p.stderr.read()
        finally:
            b.close()


def test_library_is_shared_by_jobs_and_exclusive_waits_for_all_of_them(cfg):
    s1, s2 = holder(cfg, "library", shared=True), holder(cfg, "library", shared=True)
    try:
        s1.expect("held")
        s2.expect("held")                       # both at once: shared
        path = pools.lock_path(cfg, "library")
        assert path == sync._lock_path(cfg)     # the old sync.lock, reused
        assert sorted(r["pid"] for r in pools.read_holders(path)) == sorted(
            [s1.p.pid, s2.p.pid])
        with pytest.raises(pools.PoolBusy):
            with pools.hold(cfg, "library", wait=False):
                pass

        events, got = [], threading.Event()

        def exclusive():
            with pools.hold(cfg, "library", wait=True, emit=events.append):
                got.set()

        t = threading.Thread(target=exclusive, daemon=True)
        t.start()
        s1.release()
        s1.finish()
        assert not got.is_set()                 # s2 still holds it
        s2.release()
        s2.finish()
        t.join(10)
        assert got.is_set()
        assert [e["event"] for e in events] == ["waiting", "acquired"]
        assert pools.read_holders(path) == []
    finally:
        s1.close()
        s2.close()


def test_lock_order_and_self_deadlock_are_refused(cfg):
    with pools.hold(cfg, "synth"):
        with pytest.raises(RuntimeError, match="lock order"):
            with pools.hold(cfg, "library", shared=True):
                pass
        with pytest.raises(RuntimeError, match="already held"):
            with pools.hold(cfg, "synth"):
                pass
        with pools.hold(cfg, pools.net_pool("royalroad")):     # later in the order: fine
            pass
    with pools.hold(cfg, "library", shared=True):
        with pools.hold(cfg, "browser"), pools.hold(cfg, "synth"):
            pass


def test_status_names_the_holder_and_counts_waiters(cfg):
    a = holder(cfg, "synth")
    b = None
    try:
        a.expect("held")
        b = holder(cfg, "synth", wait=True)
        b.expect("waiting")
        st = {p["pool"]: p for p in pools.status(cfg)}
        assert st["synth"]["held"] and st["synth"]["holders"][0]["pid"] == a.p.pid
        if os.path.exists("/proc/locks"):
            assert st["synth"]["waiting"] == 1
        assert not st["library"]["held"] and not st["browser"]["held"]
    finally:
        a.close()
        if b:
            b.close()


def test_progress_lists_the_pools(cfg):
    from webnovel_audio import progress

    DB(cfg.library.state_db).close()
    with pools.hold(cfg, "library", shared=True, what="sync @all"):
        snap = progress.snapshot(cfg)
        assert snap["running"] is True
        lib = next(p for p in snap["pools"] if p["pool"] == "library")
        assert lib["holders"][0]["what"] == "sync @all"
        assert "sync @all" in progress.render_text(snap)
    assert progress.snapshot(cfg)["running"] is False


# -- the pipeline ----------------------------------------------------------------

def _library(tmp_path, monkeypatch, *slugs):
    """Tracked Royal Road series with chapters 4-6 brought by "the browser";
    returns the config path."""
    from webnovel_audio import providers

    base = open(FICTION, encoding="utf-8").read()
    pages = {}
    for i, slug in enumerate(slugs):
        sid = str(424242 + i)
        pages[sid] = (base.replace("424242", sid).replace("salvage-run", slug)
                      .replace("Salvage Run", slug.replace("-", " ").title()))
    monkeypatch.setattr(providers.RoyalRoadProvider, "_get",
                        lambda self, url, ctx: next(v for k, v in pages.items() if k in url))
    monkeypatch.setattr(sync, "_cache_cover", lambda *a, **k: None)
    cfgp = tmp_path / "config.toml"
    cfgp.write_text(f'[library]\nstate_db = "{tmp_path / "state.db"}"\n'
                    f'library_dir = "{tmp_path / "lib"}"\n[synth]\nbackend = "null"\n')
    cfg = Config.load(str(cfgp))
    for i, slug in enumerate(slugs):
        sync.add_series(cfg, f"https://www.royalroad.com/fiction/{424242 + i}/{slug}",
                        start="3", log=lambda *_: None)
        db = DB(cfg.library.state_db)
        s = db.get_series(slug)
        for c in db.range(s["id"], 4, 6):
            page = CHAPTER_STUB.replace(
                "<head>", '<head><meta property="og:url" content="https://www.royalroad.com/'
                f'fiction/{s["source_id"]}/{slug}/chapter/{c["source_id"]}/x">', 1)
            assert sync.ingest_raw(cfg, db, page)["result"] == "ingested"
        db.close()
    return str(cfgp), cfg


pipeline_fixture = pytest.mark.skipif(not os.path.exists(FICTION), reason="fixture missing")


@pipeline_fixture
def test_two_renders_of_different_series_take_turns_on_synth(tmp_path, monkeypatch):
    cfgp, cfg = _library(tmp_path, monkeypatch, "salvage-run", "other-run")
    a, b = Child(RENDER, cfgp, "salvage-run", ""), Child(RENDER, cfgp, "other-run", "")
    try:
        ea, eb = a.finish(), b.finish()
    finally:
        a.close()
        b.close()
    assert a.p.returncode == 0 and b.p.returncode == 0, (ea, eb)
    for ev in (ea, eb):
        assert next(e for e in ev if e["event"] == "result")["rendered"] == 3
    spans = sorted((e["t0"], e["t1"]) for e in ea + eb if e["event"] == "span")
    assert len(spans) == 6
    for (_, end), (start, _) in zip(spans, spans[1:]):
        assert start >= end, spans                      # never two synths at once


@pipeline_fixture
def test_two_declarative_runs_render_each_chapter_once(tmp_path, monkeypatch):
    cfgp, cfg = _library(tmp_path, monkeypatch, "salvage-run")
    a, b = Child(RENDER, cfgp, "salvage-run", ""), Child(RENDER, cfgp, "salvage-run", "")
    try:
        ea, eb = a.finish(), b.finish()
    finally:
        a.close()
        b.close()
    assert a.p.returncode == 0 and b.p.returncode == 0
    done = sorted(e["number"] for e in ea + eb
                  if e["event"] == "chapter" and e["result"] == "ok")
    assert done == [4, 5, 6]
    skipped = [e for e in ea + eb if e["event"] == "chapter" and e["result"] == "skipped"]
    assert all("another run" in e["reason"] for e in skipped)
    db = DB(cfg.library.state_db)
    s = db.get_series("salvage-run")
    assert [c["status"] for c in db.range(s["id"], 4, 6)] == ["rendered"] * 3
    db.close()


@pipeline_fixture
@pytest.mark.parametrize("pool", ["synth", "library"])
def test_no_wait_exits_2_with_a_locked_event_naming_the_pool(tmp_path, monkeypatch, capsys,
                                                             pool):
    from webnovel_audio import cli

    cfgp, cfg = _library(tmp_path, monkeypatch, "salvage-run")
    a = holder(cfg, pool)                       # library: exclusive, like a cache clear
    try:
        a.expect("held")
        capsys.readouterr()
        rc = cli.main(["render", "--target", "salvage-run", "--range", "4",
                       "--no-wait", "--json", "-c", cfgp])
        out = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert rc == 2
        assert out[-1]["event"] == "locked" and out[-1]["pool"] == pool
        assert out[-1]["holder"]["pid"] == a.p.pid
    finally:
        a.close()
    db = DB(cfg.library.state_db)
    assert db.range(db.get_series("salvage-run")["id"], 4, 4)[0]["status"] != "rendered"
    db.close()


@pipeline_fixture
def test_a_render_waits_for_synth_and_says_so(tmp_path, monkeypatch, capsys):
    from webnovel_audio import cli

    cfgp, cfg = _library(tmp_path, monkeypatch, "salvage-run")
    a = holder(cfg, "synth")
    out: list[dict] = []
    try:
        a.expect("held")
        capsys.readouterr()
        rc_box = []
        t = threading.Thread(target=lambda: rc_box.append(cli.main(
            ["render", "--target", "salvage-run", "--range", "4", "--json", "-c", cfgp])),
            daemon=True)
        t.start()
        # the waiting line reaches stdout before the render blocks
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and '"waiting"' not in capsys.readouterr().out:
            time.sleep(0.02)
        a.release()
        t.join(30)
        assert rc_box == [0]
    finally:
        a.close()
    db = DB(cfg.library.state_db)
    assert db.range(db.get_series("salvage-run")["id"], 4, 4)[0]["status"] == "rendered"
    db.close()


@pipeline_fixture
def test_no_write_transaction_is_open_across_synthesis(tmp_path, monkeypatch):
    cfgp, cfg = _library(tmp_path, monkeypatch, "salvage-run")
    seen = []
    real_prepare = sync._prepare_render

    def prepare(cfg_, db, *a, **k):
        seen.append(db.con.in_transaction)
        return real_prepare(cfg_, db, *a, **k)

    monkeypatch.setattr(sync, "_prepare_render", prepare)
    res = sync.run_stage(cfg, "rendered", "salvage-run", backend="null", log=lambda *_: None)
    assert res.rendered == 3 and seen == [False, False, False]


def test_a_fetch_holds_its_site_pool_with_no_transaction_open(tmp_path):
    """`_do_fetch` takes `net-<provider>` around the page fetch, and nothing in
    the DB is mid-write while it waits on the network."""
    db = DB(str(tmp_path / "s.db"))
    cfg = Config()
    cfg.library.state_db = str(tmp_path / "s.db")
    from webnovel_audio.providers import SeriesInfo, ChapterRef
    sid = db.upsert_series(SeriesInfo(provider="royalroad", source_id="9", slug="demo",
                                      title="Demo", url="https://rr/9"))
    db.replace_chapters(sid, [ChapterRef(source_id="100", order=0, title="C", slug="c",
                                         url="https://rr/c/100", unlocked=True)])
    c = db.chapters(sid)[0]
    held = []

    class Prov:
        name, raw_ext = "royalroad", ".html"

        def check_fetchable(self, ref, ctx):
            pass

        def fetch(self, ref, ctx):
            held.append((pools.status(cfg), db.con.in_transaction))
            return "<html></html>"

    def hold(name, what=""):
        return pools.hold(cfg, name, what=what)

    sync._do_fetch(cfg, db, Prov(), None, str(tmp_path / "b"), c, hold=hold)
    st, in_tx = held[0]
    net = next(p for p in st if p["pool"] == "net-royalroad")
    assert net["held"] and net["holders"][0]["what"] == "fetch #1" and in_tx is False
    db.close()


# -- DB --------------------------------------------------------------------------

def test_every_db_write_method_leaves_no_transaction_open(tmp_path):
    from webnovel_audio.providers import ChapterRef, SeriesInfo, VolumeRef

    db = DB(str(tmp_path / "s.db"))
    con = db.con
    checked = []

    def ok(name, *a, **k):
        getattr(db, name)(*a, **k)
        assert not con.in_transaction, name
        checked.append(name)

    assert not con.in_transaction
    fi = SeriesInfo(provider="royalroad", source_id="9", slug="demo", title="Demo",
                    author="A", url="https://rr/9")
    sid = db.upsert_series(fi)
    assert not con.in_transaction
    checked.append("upsert_series")
    chs = [ChapterRef(source_id=str(100 + i), order=i, title=f"C{i}", slug=f"c{i}",
                      url=f"https://rr/c/{100 + i}", published_at="2026-01-01",
                      unlocked=True) for i in range(3)]
    ok("replace_chapters", sid, chs)
    ok("replace_volumes", sid, [VolumeRef(source_id="1", title="Book One", order=0)])
    ok("set_series_credits", sid, "B", "https://rr/b", None)
    ok("set_provider_state", sid, {"k": 1})
    ok("set_bundle", sid, str(tmp_path / "lib" / "demo"))
    ok("rename_series", sid, "demo2")
    c0 = db.chapters(sid)[0]
    ok("mark_stage", c0["id"], "fetched", raw_path="/x")
    ok("mark", c0["id"], "error", error="boom", error_stage="rendered")
    ok("set_status", [c0["id"]], "new")
    ok("set_priority", sid, 5)
    ok("set_enabled", sid, False)
    ok("add_profile", "me")
    ok("save_position", "me", sid, chs[0].source_id, 1.5, chs[1].source_id)
    ok("remove_profile", "me")
    ok("forget", sid)
    db.close()
    public_writers = {n for n in dir(DB) if n.startswith(("set_", "replace_", "mark",
                                                          "upsert_", "rename_", "add_",
                                                          "remove_", "save_", "forget"))}
    assert public_writers <= set(checked), public_writers - set(checked)


# -- grab ------------------------------------------------------------------------

@pipeline_fixture
def test_grab_waiting_for_synth_is_not_a_stall(tmp_path, monkeypatch):
    from test_grab import Browser, _open_series, _page, _run_to_done, _start

    from webnovel_audio import providers
    from webnovel_audio.grab import GrabLoop

    fiction = open(FICTION, encoding="utf-8").read()
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
    box = {}

    def render(series_id, number):                 # the real pool, a fake synthesis
        with pools.hold(cfg, "synth", emit=box["loop"]._render_emit):
            d = DB(cfg.library.state_db)
            row = d.con.execute("SELECT id FROM chapters WHERE series_id=? AND ord=?",
                                (series_id, number - 1)).fetchone()
            d.mark(row["id"], "rendered")
            d.close()
        return type("R", (), {"rendered": 1, "errors": 0})()

    events = []
    loop = box["loop"] = GrabLoop(cfg, "salvage-run", 1, emit=events.append, render=render,
                                  stall_timeout=0.4)
    a = holder(cfg, "synth")
    t = None
    try:
        a.expect("held")
        t = _start(loop)
        b = Browser(loop)
        _open_series(b)
        nxt = b.call("GET", "/next")[1]
        assert nxt["action"] == "fetch" and nxt["number"] == 1
        assert b.call("POST", "/page", {"html": _page(ids[0])})[1]["ok"]
        deadline = time.monotonic() + 10
        while not any(e.get("event") == "waiting" for e in events):
            assert time.monotonic() < deadline, events
            time.sleep(0.02)
        # queued for well past the stall timeout: still not given up on
        until = time.monotonic() + 1.2
        while time.monotonic() < until:
            assert not loop.finished, loop.finished
            time.sleep(0.05)
        a.release()
        nxt = _run_to_done(loop, b)
        assert nxt["reason"] == "every series done", nxt
        assert loop.rendered == 1 and loop.errors == 0
        assert any(e.get("event") == "acquired" for e in events)
    finally:
        loop.finish("test over")
        a.close()
        if t:
            t.join(10)


@pipeline_fixture
def test_grab_stopped_while_queued_for_synth_ends_without_rendering(tmp_path, monkeypatch):
    from test_grab import Browser, _open_series, _page, _start

    from webnovel_audio import providers
    from webnovel_audio.grab import GrabLoop

    fiction = open(FICTION, encoding="utf-8").read()
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
    box, rendered = {}, []

    def render(series_id, number):
        with pools.hold(cfg, "synth", emit=box["loop"]._render_emit):
            rendered.append(number)
        return type("R", (), {"rendered": 1, "errors": 0})()

    events = []
    loop = box["loop"] = GrabLoop(cfg, "salvage-run", 1, emit=events.append, render=render)
    a = holder(cfg, "synth")
    try:
        a.expect("held")
        t = _start(loop)
        b = Browser(loop)
        _open_series(b)
        b.call("GET", "/next")
        assert b.call("POST", "/page", {"html": _page(ids[0])})[1]["ok"]
        deadline = time.monotonic() + 10
        while not any(e.get("event") == "waiting" for e in events):
            assert time.monotonic() < deadline, events
            time.sleep(0.02)
        loop.finish("stopped")                          # SIGTERM / Stop in the UI
        t.join(10)
        assert not t.is_alive() and rendered == []
        assert any("not started" in e.get("message", "") for e in events)
    finally:
        a.close()
