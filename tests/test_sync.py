import os

import pytest

from webnovel_audio import sync
from webnovel_audio.config import Config
from webnovel_audio.db import DB
from webnovel_audio.royalroad import ChapterRef, FictionInfo

FICTION_FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "samples", "salvage-run-fiction.html"
)
CHAPTER_STUB = """<html><head><meta property="og:title" content="4. Deck Four - Salvage Run">
</head><body><div class="chapter-content">
<p>The corridor ended at a sealed door.</p><p>"Careful," Resk said.</p>
</div></body></html>"""


def _chapters(n=5):
    return [
        ChapterRef(rr_id=str(100 + i), order=i, title=f"Chapter {i + 1}",
                   slug=f"ch-{i + 1}", url=f"https://rr/chapter/{100 + i}/ch-{i + 1}",
                   published_at="2026-01-01", unlocked=True)
        for i in range(n)
    ]


def test_db_roundtrip_and_pending(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    fi = FictionInfo(rr_id="9", slug="demo", title="Demo", author="A", url="https://rr/9")
    sid = db.upsert_series(fi)
    assert db.replace_chapters(sid, _chapters(5)) == 5
    assert db.replace_chapters(sid, _chapters(7)) == 2      # 2 new, idempotent for the rest

    db.force_progress(sid, 2)                                # heard through #3
    pend = db.pending(sid)
    assert [c["ord"] for c in pend] == [3, 4, 5, 6]

    db.mark(pend[0]["id"], "rendered", audio_path="/x/004.opus")
    db.set_progress(sid, 3)
    assert [c["ord"] for c in db.pending(sid)] == [4, 5, 6]
    db.close()


def test_parse_range():
    from webnovel_audio.cli import _parse_range

    assert _parse_range("") == (None, None)
    assert _parse_range("5") == (5, 5)
    assert _parse_range("3-7") == (3, 7)
    assert _parse_range("4-") == (4, None)
    assert _parse_range("-6") == (None, 6)


def test_series_redo(tmp_path, capsys):
    import json
    import types

    from webnovel_audio import cli
    from webnovel_audio.royalroad import ChapterRef, FictionInfo

    dbp = tmp_path / "s.db"
    db = DB(str(dbp))
    fi = FictionInfo(rr_id="9", slug="demo", title="Demo", url="https://rr/9")
    sid = db.upsert_series(fi)
    db.replace_chapters(sid, [
        ChapterRef(rr_id=str(100 + i), order=i, title=f"C{i + 1}", slug=f"c{i + 1}",
                   url=f"https://rr/c/{100 + i}", published_at="2025-01-01")
        for i in range(6)
    ])
    for c in db.chapters(sid)[:4]:               # #1-#4 rendered
        db.mark(c["id"], "rendered", audio_path="/x.opus")
    db.force_progress(sid, 3)
    db.close()

    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[royalroad]\nstate_db = "{dbp}"\n')
    cli._cmd_series(types.SimpleNamespace(action="redo", key="demo", range="2-3",
                                          config=str(cfg), json=True))
    out = json.loads(capsys.readouterr().out)
    assert out["requeued"] == [2, 3]

    db = DB(str(dbp))
    s = db.get_series("demo")
    assert s["progress_order"] == 0                              # rewound to before #2
    st = {c["ord"] + 1: c["status"] for c in db.chapters(s["id"])}
    assert st[2] == "new" and st[3] == "new" and st[1] == "rendered" and st[4] == "rendered"
    assert [c["ord"] + 1 for c in db.pending(s["id"])] == [2, 3, 5, 6]
    db.close()


def test_sync_lock_blocks_a_second_holder(tmp_path):
    cfg = Config()
    cfg.royalroad.state_db = str(tmp_path / "s.db")
    with sync.sync_lock(cfg):
        with pytest.raises(sync.SyncLocked):
            with sync.sync_lock(cfg):
                pass                                    # never reached
    with sync.sync_lock(cfg):                            # released -> free again
        pass


def test_cmd_sync_refuses_when_locked(tmp_path, capsys):
    import json
    import types

    from webnovel_audio import cli

    cfgp = tmp_path / "c.toml"
    dbp = tmp_path / "s.db"
    cfgp.write_text(f'[royalroad]\nstate_db = "{dbp}"\n')
    cfg = Config.load(str(cfgp))
    with sync.sync_lock(cfg):
        rc = cli._cmd_sync(types.SimpleNamespace(
            config=str(cfgp), key=None, limit=None, dry_run=False,
            backend=None, no_refresh=False, json=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["event"] == "locked"


def test_resolve_start_variants():
    chs = _chapters(10)
    assert sync._resolve_start(chs, "latest") == 9
    assert sync._resolve_start(chs, "start") == -1
    assert sync._resolve_start(chs, "4") == 3
    assert sync._resolve_start(chs, "https://rr/chapter/103/ch-4") == 3


def test_series_cfg_prefers_per_series_lexicon(tmp_path):
    cfg = Config()
    cfg.general.lexicon_dir = str(tmp_path)
    cfg.general.series_config_dir = str(tmp_path)
    assert sync._series_cfg(cfg, "salvage-run") is cfg          # no file -> unchanged
    (tmp_path / "salvage-run.csv").write_text("surface,respell,ipa,notes\n")
    sc = sync._series_cfg(cfg, "salvage-run")
    assert sc is not cfg and sc.general.lexicon.endswith("salvage-run.csv")
    assert cfg.general.lexicon == ""                          # original untouched


def test_series_cfg_applies_toml_overlay(tmp_path):
    cfg = Config()
    cfg.general.series_config_dir = str(tmp_path)
    (tmp_path / "salvage-run.toml").write_text(
        '[cast]\nprotagonist = "Mara"\n[cast.voices]\nMara = "af_heart"\n'
        '[pauses]\nlead_ms = 1500\n[dsp.Mara]\nsemitones = -1.0\n'
    )
    sc = sync._series_cfg(cfg, "salvage-run")
    assert sc is not cfg
    assert sc.cast.protagonist == "Mara" and sc.cast.voices == {"Mara": "af_heart"}
    assert sc.pauses.lead_ms == 1500
    assert sc.dsp["Mara"] == {"semitones": -1.0} and "thought" in sc.dsp   # base kept
    assert cfg.cast.protagonist == "" and cfg.pauses.lead_ms == 1000       # base untouched


def test_add_and_sync_offline(tmp_path, monkeypatch):
    from webnovel_audio import providers

    if not os.path.exists(FICTION_FIXTURE):
        return
    fiction_html = open(FICTION_FIXTURE, encoding="utf-8").read()
    # intercept the RoyalRoad provider's only network method
    monkeypatch.setattr(
        providers.RoyalRoadProvider, "raw",
        lambda self, url, *, cfg: fiction_html if "/chapter/" not in url else CHAPTER_STUB,
    )
    monkeypatch.setattr(sync, "_cache_cover", lambda *a, **k: None)

    cfg = Config()
    cfg.royalroad.state_db = str(tmp_path / "state.db")
    cfg.royalroad.library_dir = str(tmp_path / "lib")

    info = sync.add_series(cfg, "https://www.royalroad.com/fiction/424242/salvage-run",
                           start="4", log=lambda *_: None)
    assert info["provider"] == "royalroad" and info["pending"] == 2
    db = DB(cfg.royalroad.state_db)
    s = db.get_series("424242")
    assert s["progress_order"] == 3
    assert len(db.pending(s["id"])) == 2                    # chapters 5, 6

    summ = db.summary()[0]
    assert summ["slug"] == "salvage-run" and summ["provider"] == "royalroad"
    assert summ["pending"] == 2 and summ["progress"] == 4
    assert summ["next"]["number"] == 5
    db.close()

    events = []
    res = sync.run_sync(cfg, limit=1, backend="null", log=lambda *_: None,
                        emit=events.append)
    assert res.rendered == 1 and res.errors == 0
    kinds = [e["event"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert "chapter_begin" in kinds
    ch = next(e for e in events if e["event"] == "chapter" and e.get("result") == "rendered")
    assert ch["number"] == 5 and "path" in ch and ch["audio_seconds"] >= 0

    db = DB(cfg.royalroad.state_db)
    s = db.get_series("424242")
    assert s["progress_order"] == 4                         # advanced by one
    done = [c for c in db.chapters(s["id"]) if c["status"] == "rendered"]
    assert len(done) == 1 and os.path.exists(done[0]["audio_path"])
    db.close()

    raw = tmp_path / "lib" / s["slug"] / ".raw"
    assert raw.is_dir() and any(raw.iterdir())              # chapter html cached
