"""`progress` reads the live state DB while a sync writes it — so the one thing
these tests must pin down is that it only ever reads."""
import os
import sqlite3
import time

import pytest

from webnovel_audio import progress
from webnovel_audio.config import Config
from webnovel_audio.db import DB
from webnovel_audio.royalroad import ChapterRef, FictionInfo

TS = progress.TS


def _cfg(tmp_path, db_path):
    cfg = Config()
    cfg.royalroad.state_db = str(db_path)
    return cfg


def _stamp(offset_s: float) -> str:
    return time.strftime(TS, time.localtime(time.time() + offset_s))


def _seed(tmp_path, *, rendered=3, in_flight=0, gap_before_last=0.0):
    """A series with `rendered` finished chapters, newest last, plus optional
    chapters that started and haven't ended (what an in-flight render looks like)."""
    path = tmp_path / "state.db"
    db = DB(str(path))
    fi = FictionInfo(rr_id="9", slug="demo", title="Demo", author="A", url="https://rr/9")
    sid = db.upsert_series(fi)
    n = rendered + in_flight
    db.replace_chapters(sid, [
        ChapterRef(rr_id=str(100 + i), order=i, title=f"Chapter {i + 1}", slug=f"c{i}",
                   url=f"https://rr/{100 + i}", published_at="2026-01-01", unlocked=True)
        for i in range(n)])
    chs = db.chapters(sid)
    con = db.con
    # oldest first, each 120 s of wall for 600 s of audio
    base = -(rendered + in_flight) * 200.0
    for i in range(rendered):
        extra = gap_before_last if i == rendered - 1 else 0.0
        t0 = base + i * 200.0 + extra
        con.execute(
            "UPDATE chapters SET status='rendered', duration_s=600, "
            "render_started_at=?, render_ended_at=?, rendered_at=?, "
            "fetched_at=?, parsed_at=? WHERE id=?",
            (_stamp(t0), _stamp(t0 + 120), _stamp(t0 + 120),
             _stamp(t0 - 20), _stamp(t0 - 10), chs[i]["id"]))
    for j in range(in_flight):
        con.execute("UPDATE chapters SET status='parsed', render_started_at=? WHERE id=?",
                    (_stamp(-30.0), chs[rendered + j]["id"]))
    con.commit()
    db.close()
    return path


def test_snapshot_shape(tmp_path):
    path = _seed(tmp_path, rendered=3)
    snap = progress.snapshot(_cfg(tmp_path, path))
    assert set(snap) >= {"now", "running", "run", "in_flight", "series",
                         "recent", "errors", "totals"}
    assert snap["run"]["inferred"] is True
    assert snap["run"]["chapters"] == 3
    assert len(snap["recent"]) == 3


def test_recent_is_newest_first_with_real_timings(tmp_path):
    path = _seed(tmp_path, rendered=3)
    snap = progress.snapshot(_cfg(tmp_path, path))
    nums = [c["number"] for c in snap["recent"]]
    assert nums == sorted(nums, reverse=True)
    c = snap["recent"][0]
    assert c["wall_seconds"] == pytest.approx(120, abs=2)
    assert c["audio_seconds"] == pytest.approx(600, abs=1)
    assert c["ratio"] == pytest.approx(0.2, abs=0.02)      # wall / audio


def test_in_flight_is_started_but_not_ended(tmp_path):
    path = _seed(tmp_path, rendered=2, in_flight=1)
    snap = progress.snapshot(_cfg(tmp_path, path))
    assert len(snap["in_flight"]) == 1
    assert snap["in_flight"][0]["elapsed_seconds"] > 0
    assert snap["in_flight"][0]["number"] not in [c["number"] for c in snap["recent"]]


def test_a_long_gap_ends_the_inferred_run(tmp_path):
    """The heuristic the `runs` table will replace: a quiet stretch splits runs."""
    path = _seed(tmp_path, rendered=3, gap_before_last=progress.RUN_GAP_SECONDS * 3)
    snap = progress.snapshot(_cfg(tmp_path, path))
    assert snap["run"]["chapters"] == 1        # only the one after the gap


def test_outstanding_and_eta(tmp_path):
    path = _seed(tmp_path, rendered=2, in_flight=2)
    snap = progress.snapshot(_cfg(tmp_path, path))
    s = snap["series"][0]
    assert s["outstanding"] == 2               # the two not yet rendered
    assert s["eta_seconds"] > 0
    assert snap["totals"]["outstanding"] == 2


def test_missing_database_raises_filenotfound(tmp_path):
    cfg = _cfg(tmp_path, tmp_path / "nope.db")
    with pytest.raises(FileNotFoundError):
        progress.snapshot(cfg)


# --- the safety property: this command must never write -------------------

def test_connection_is_read_only(tmp_path):
    path = _seed(tmp_path, rendered=1)
    con = progress.open_ro(_cfg(tmp_path, path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("UPDATE chapters SET title='nope'")
        con.execute("SELECT 1").fetchone()          # reads still fine
    finally:
        con.close()


def test_snapshot_does_not_modify_the_database(tmp_path):
    path = _seed(tmp_path, rendered=3)
    before = (os.path.getsize(path), open(path, "rb").read())
    progress.snapshot(_cfg(tmp_path, path))
    after = (os.path.getsize(path), open(path, "rb").read())
    assert before == after


def test_lock_probe_reports_free_and_held(tmp_path):
    import fcntl

    from webnovel_audio.sync import _lock_path

    path = _seed(tmp_path, rendered=1)
    cfg = _cfg(tmp_path, path)
    lock = _lock_path(cfg)
    os.makedirs(os.path.dirname(lock) or ".", exist_ok=True)
    open(lock, "w").close()
    assert progress.sync_running(cfg) is False
    held = open(lock, "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert progress.sync_running(cfg) is True
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()
    assert progress.sync_running(cfg) is False


def test_render_text_runs_on_an_empty_library(tmp_path):
    path = tmp_path / "state.db"
    DB(str(path)).close()
    out = progress.render_text(progress.snapshot(_cfg(tmp_path, path)))
    assert "outstanding" in out
