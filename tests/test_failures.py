"""What `run_stage` does with the two kinds of failure: a bad chapter and a bad
environment (see docs/plans/grab-robustness.md)."""
import os

import pytest

from webnovel_audio import providers, sync, tagger
from webnovel_audio.config import Config
from webnovel_audio.db import DB
from webnovel_audio.errors import ContentError, DependencyMissing

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "samples", "salvage-run-fiction.html")
pytestmark = pytest.mark.skipif(not os.path.exists(FIXTURE), reason="fixture missing")


@pytest.fixture
def held(tmp_path, monkeypatch):
    """A series whose chapters 1-3 the browser has already brought."""
    fiction = open(FIXTURE, encoding="utf-8").read()
    monkeypatch.setattr(providers.RoyalRoadProvider, "_get", lambda self, url, ctx: fiction)
    monkeypatch.setattr(sync, "_cache_cover", lambda *a, **k: None)
    cfg = Config()
    cfg.library.state_db = str(tmp_path / "state.db")
    cfg.library.library_dir = str(tmp_path / "lib")
    cfg.synth.backend = "null"
    sync.add_series(cfg, "https://www.royalroad.com/fiction/424242/salvage-run",
                    start="start", log=lambda *_: None)
    db = DB(cfg.library.state_db)
    s = db.get_series("salvage-run")
    for c in db.range(s["id"], 1, 3):
        page = ('<html><head><meta property="og:url" content="https://www.royalroad.com/'
                f'fiction/424242/salvage-run/chapter/{c["source_id"]}/x"></head><body>'
                '<div class="chapter-content"><p>The corridor ended.</p></div></body></html>')
        assert sync.ingest_raw(cfg, db, page)["result"] == "ingested"
    db.close()
    return cfg


def _status(cfg, lo=1, hi=3):
    db = DB(cfg.library.state_db)
    out = [c["status"] for c in db.range(db.get_series("salvage-run")["id"], lo, hi)]
    db.close()
    return out


def test_a_missing_dependency_stops_the_run_and_marks_nothing(held, monkeypatch):
    monkeypatch.setattr(tagger, "available", lambda model=tagger.DEFAULT_MODEL: False)
    events = []
    with pytest.raises(DependencyMissing, match="POS tagger"):
        sync.run_stage(held, "rendered", "salvage-run", backend="null",
                       log=lambda *_: None, emit=events.append)
    assert _status(held) == ["fetched"] * 3                  # none stamped `error`
    assert not any(e["event"] == "chapter_begin" for e in events)


def test_a_dependency_failing_mid_run_aborts_with_an_event_and_marks_nothing(held, monkeypatch):
    def broken(*a, **k):
        raise DependencyMissing("ffmpeg not found on PATH")

    monkeypatch.setattr(sync.pipeline, "master", broken)
    events = []
    with pytest.raises(DependencyMissing):
        sync.run_stage(held, "rendered", "salvage-run", backend="null",
                       log=lambda *_: None, emit=events.append)
    assert "error" not in _status(held)
    assert any(e["event"] == "abort" and "ffmpeg" in e["reason"] for e in events)


def test_a_bad_chapter_is_marked_and_skipped_and_retried_next_run(held, monkeypatch):
    real = sync._do_parse

    def parse(cfg, db, prov, ctx, bdir, c, raw_path, **kw):
        if c["ord"] == 0:
            raise ContentError("could not locate chapter content in the page")
        return real(cfg, db, prov, ctx, bdir, c, raw_path, **kw)

    monkeypatch.setattr(sync, "_do_parse", parse)
    res = sync.run_stage(held, "rendered", "salvage-run", backend="null", log=lambda *_: None)
    assert res.errors == 1 and res.rendered == 2
    assert _status(held) == ["error", "rendered", "rendered"]

    monkeypatch.setattr(sync, "_do_parse", real)             # the cause was ephemeral
    res = sync.run_stage(held, "rendered", "salvage-run", backend="null", log=lambda *_: None)
    assert res.errors == 0 and res.rendered == 1
    assert _status(held) == ["rendered"] * 3
