import http.client
import json
import threading

import pytest

import webnovel_audio.serve as srv
from webnovel_audio.config import Config
from webnovel_audio.db import DB
from webnovel_audio.providers import ChapterRef, SeriesInfo


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A running server over a tiny library: `demo` (3 rendered chapters + 1 not),
    and a paused `quiet` series, so the api has something real to describe."""
    cfg = Config()
    cfg.library.state_db = str(tmp_path / "s.db")
    cfg.library.library_dir = str(tmp_path / "lib")
    db = DB(cfg.library.state_db)

    def add(source_id, slug, title, n, rendered, enabled=True):
        sid = db.upsert_series(SeriesInfo(provider="royalroad", source_id=source_id, slug=slug,
                                          title=title, author="An Author", url="https://rr/x"))
        db.replace_chapters(sid, [ChapterRef(source_id=f"{source_id}{i}", order=i,
                                             title=f"Ch {i + 1}", slug="c", url="https://rr/c",
                                             published_at="2026-09-01T00:00:00Z", unlocked=True)
                                  for i in range(n)])
        for i, c in enumerate(db.chapters(sid)):
            if i < rendered:
                f = tmp_path / f"{source_id}-{i + 1:03d}.opus"
                f.write_bytes(b"OggS" + b"\0" * 64)
                db.mark(c["id"], "rendered", audio_path=str(f), duration_s=600.0 + i)
        if not enabled:
            db.set_enabled(sid, False)
        return sid

    add("1", "demo", "Demo", 4, 3)
    add("2", "quiet", "Quiet", 2, 2, enabled=False)
    add("3", "unrendered", "Nothing Yet", 2, 0)
    db.close()

    web = tmp_path / "web"
    web.mkdir()
    (web / "player.html").write_text("<!doctype html><title>p</title>")
    (web / "app.css").write_text("body{}")
    (web / "evil.exe").write_text("x")
    monkeypatch.setattr(srv, "WEB_DIR", str(web))

    httpd = srv._Server(("127.0.0.1", 0), srv._Handler)
    httpd.cfg = cfg
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def call(method, path, body=None, ctype="application/json", raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        headers = {"Content-Type": ctype} if data is not None else {}
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        try:
            return resp, json.loads(payload)
        except ValueError:
            return resp, payload

    call.db = cfg.library.state_db
    yield call
    httpd.shutdown()
    httpd.server_close()


def test_library_lists_only_playable_chapters_in_order_and_keeps_paused_series(env):
    resp, lib = env("GET", "/api/library")
    assert resp.status == 200 and resp.getheader("Cache-Control") == "no-store"
    slugs = [s["slug"] for s in lib["series"]]
    assert slugs == ["demo", "quiet"]                       # sorted by title; nothing-yet is out
    demo = lib["series"][0]
    assert [c["n"] for c in demo["chapters"]] == [1, 2, 3]  # the 4th was never rendered
    c = demo["chapters"][0]
    assert c["id"] == "10" and c["audio"] == "/audio/demo/1-001.opus" and c["dur"] == 600
    assert demo["paused"] is False and lib["series"][1]["paused"] is True


def test_profiles_are_created_by_name_without_a_login(env):
    assert env("GET", "/api/profiles")[1] == {"profiles": []}
    resp, body = env("POST", "/api/profiles", {"name": "  Jon  "})
    assert resp.status == 200 and body == {"profile": "Jon"}
    assert env("POST", "/api/profiles", {"name": "jon"})[1] == {"profile": "Jon"}   # same person
    env("POST", "/api/profiles", {"name": "Mia"})
    assert env("GET", "/api/profiles")[1] == {"profiles": ["Jon", "Mia"]}
    for bad in ("", "x" * 40, "<script>", "../etc", None, 5):
        assert env("POST", "/api/profiles", {"name": bad})[0].status == 400, bad


def test_writes_must_be_json_and_small(env):
    # a cross-site form post can only send these types, and cannot send application/json
    assert env("POST", "/api/profiles", ctype="text/plain", raw=b'{"name":"Jon"}')[0].status == 415
    assert env("POST", "/api/profiles", ctype="application/x-www-form-urlencoded",
               raw=b"name=Jon")[0].status == 415
    assert env("POST", "/api/profiles", raw=b"{nope")[0].status == 400
    assert env("POST", "/api/profiles", raw=b"null")[0].status == 400     # answered, not hung
    assert env("POST", "/api/profiles", raw=b'{"name":"' + b"a" * 5000 + b'"}')[0].status == 413
    resp, _ = env("POST", "/api/profiles", {"name": "Jon"})
    assert resp.getheader("Access-Control-Allow-Origin") is None          # never any CORS
    assert env("POST", "/nope", {"a": 1})[0].status == 404


def test_positions_follow_a_profile_and_furthest_only_moves_forward(env):
    env("POST", "/api/profiles", {"name": "Jon"})
    assert env("GET", "/api/positions/Jon")[1] == {"profile": "Jon", "series": {}}

    put = lambda **b: env("PUT", "/api/positions/Jon/demo", b)
    resp, pos = put(chapter="11", t=30.5, furthest="11")
    assert resp.status == 200 and pos["chapter"] == "11" and pos["t"] == 30.5
    pos = put(chapter="12", t=5, furthest="12")[1]
    assert pos["furthest"] == "12"
    # going back to an old chapter moves *current* only; another device sending an
    # older furthest cannot wind it back either
    pos = put(chapter="10", t=0, furthest="10")[1]
    assert pos["chapter"] == "10" and pos["furthest"] == "12"
    assert put(chapter="10", t=1)[1]["furthest"] == "12"                  # none sent: unchanged
    assert env("GET", "/api/positions/Jon")[1]["series"]["demo"]["furthest"] == "12"
    assert pos["t"] == 0 and pos["updated"]


def test_furthest_remembers_where_you_were_in_it(env):
    env("POST", "/api/profiles", {"name": "Jon"})
    put = lambda **b: env("PUT", "/api/positions/Jon/demo", b)[1]
    assert put(chapter="12", t=40, furthest="12")["furthest_t"] == 40      # still in it: live
    assert put(chapter="12", t=95, furthest="12")["furthest_t"] == 95
    back = put(chapter="10", t=7, furthest="10")                          # scrolled back
    assert back["chapter"] == "10" and back["furthest"] == "12" and back["furthest_t"] == 95
    assert put(chapter="10", t=300)["furthest_t"] == 95                    # still listening back there
    assert put(chapter="12", t=2, furthest="12")["furthest_t"] == 2        # return to it
    assert put(chapter="12", t=5, furthest="11")["furthest"] == "12"       # (11 is older: no change)
    assert env("PUT", "/api/positions/Jon/demo", {"chapter": "12", "t": 1, "furthest": "12"})[1]["furthest_t"] == 1


def test_positions_reject_unknown_things_and_clamp_time(env):
    env("POST", "/api/profiles", {"name": "Jon"})
    assert env("GET", "/api/positions/Nobody")[0].status == 404
    assert env("PUT", "/api/positions/Nobody/demo", {"chapter": "10"})[0].status == 404
    assert env("PUT", "/api/positions/Jon/nope", {"chapter": "10"})[0].status == 400
    assert env("PUT", "/api/positions/Jon/demo", {"chapter": "999"})[0].status == 400
    assert env("PUT", "/api/positions/Jon/demo", {"chapter": "10", "furthest": "999"})[0].status == 400
    assert env("PUT", "/api/positions/Jon/demo", {"chapter": 10})[0].status == 400
    assert env("PUT", "/api/positions/Jon/demo", {"chapter": "10", "t": "soon"})[0].status == 400
    assert env("PUT", "/api/positions/Jon/demo", {"chapter": "10", "t": -50})[1]["t"] == 0
    assert env("PUT", "/api/positions/Jon/demo", {"chapter": "10", "t": 1e12})[1]["t"] == 7 * 24 * 3600
    # a chapter id from another series is not valid here
    assert env("PUT", "/api/positions/Jon/demo", {"chapter": "20"})[0].status == 400


def test_the_player_is_the_home_page(env):
    for path in ("/", "/index.html", "/player", "/player/"):
        resp, body = env("GET", path)
        assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/html"), path
        assert b"<title>p</title>" in body                        # the (test) player shell


def test_static_player_files_are_served_from_the_web_dir_only(env):
    resp, body = env("GET", "/player/")
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/html")
    assert env("GET", "/player")[0].status == 200
    resp, _ = env("GET", "/player/app.css")
    assert resp.getheader("Content-Type").startswith("text/css")
    for bad in ("/player/evil.exe", "/player/missing.js", "/player/..%2fserve.py",
                "/player/../serve.py", "/player/.hidden", "/player/a/b.css"):
        assert env("GET", bad)[0].status == 404, bad


def test_a_renderer_holding_the_write_lock_never_wedges_the_server(env, monkeypatch):
    """Server + render share one state DB. While the renderer holds the write lock
    a position save must answer 503 (and the page retries), within a bounded wait;
    reads must not wait at all; and once the lock is released saves work again."""
    import sqlite3
    import time

    monkeypatch.setattr(srv, "DB_BUSY_MS", 300)
    env("POST", "/api/profiles", {"name": "Jon"})
    renderer = sqlite3.connect(env.db, timeout=5)
    renderer.execute("BEGIN IMMEDIATE")
    renderer.execute("UPDATE chapters SET duration_s = duration_s")
    try:
        t0 = time.time()
        resp, body = env("PUT", "/api/positions/Jon/demo", {"chapter": "10", "t": 5, "furthest": "10"})
        assert resp.status == 503 and resp.getheader("Retry-After") == "2"
        assert body == {"error": "busy, try again"} and time.time() - t0 < 3
        t0 = time.time()
        assert env("GET", "/api/library")[0].status == 200          # WAL: readers never wait
        assert env("GET", "/api/positions/Jon")[0].status == 200
        assert env("GET", "/feed/demo.xml")[0].status == 200
        assert time.time() - t0 < 1
    finally:
        renderer.commit()
        renderer.close()
    assert env("PUT", "/api/positions/Jon/demo", {"chapter": "10", "t": 5, "furthest": "10"})[0].status == 200


def test_concurrent_saves_from_several_devices_never_wind_furthest_back(tmp_path):
    import random
    import threading

    from webnovel_audio.db import DB
    from webnovel_audio.providers import ChapterRef, SeriesInfo

    path = str(tmp_path / "s.db")
    db = DB(path)
    sid = db.upsert_series(SeriesInfo(provider="royalroad", source_id="1", slug="demo",
                                      title="Demo", url="https://rr/x"))
    db.replace_chapters(sid, [ChapterRef(source_id=f"c{i}", order=i, title="t", slug="c",
                                         url="u", published_at="", unlocked=True)
                              for i in range(30)])
    db.add_profile("Jon")
    db.close()

    errors = []

    def device(seed):
        rng = random.Random(seed)
        conn = DB(path, init=False, busy_ms=10_000)
        for _ in range(40):
            k = rng.randrange(30)
            try:
                conn.save_position("Jon", sid, f"c{k}", float(k), f"c{k}")
            except Exception as exc:                                  # noqa: BLE001
                errors.append(exc)
        conn.close()

    threads = [threading.Thread(target=device, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    final = DB(path).get_positions("Jon")[sid]
    # the furthest ever sent by any device is the highest index any of them drew
    top = 0
    for i in range(8):
        rng = random.Random(i)                   # the same draws that device made
        top = max([top] + [rng.randrange(30) for _ in range(40)])
    assert final["furthest"] == f"c{top}"


def test_a_request_connection_does_not_run_the_schema_pass(tmp_path):
    import sqlite3

    from webnovel_audio.db import DB

    DB(str(tmp_path / "x.db"), init=False).close()
    tables = sqlite3.connect(str(tmp_path / "x.db")).execute(
        "SELECT name FROM sqlite_master").fetchall()
    assert tables == []                       # nothing was created, so nothing was locked for
