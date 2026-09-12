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

    # 0.2: status alone gates work. The reader's progress marker is NOT consulted
    # (when it was, a 'new' chapter behind it became invisible to every command).
    db.force_progress(sid, 2)
    assert [c["ord"] for c in db.pending(sid)] == [0, 1, 2, 3, 4, 5, 6]

    chs = db.chapters(sid)
    db.set_status([c["id"] for c in chs[:3]], "skipped")     # explicitly not wanted
    assert [c["ord"] for c in db.pending(sid)] == [3, 4, 5, 6]

    db.mark(chs[3]["id"], "rendered", audio_path="/x/004.opus")
    assert [c["ord"] for c in db.pending(sid)] == [4, 5, 6]

    # a mid-pipeline chapter is still outstanding for render, but not for fetch
    db.mark(chs[4]["id"], "fetched", raw_path="/x/005.html")
    assert [c["ord"] for c in db.pending(sid)] == [4, 5, 6]
    assert [c["ord"] for c in db.outstanding(sid, "fetched")] == [5, 6]

    # an error is retried, and remembers which stage broke
    db.mark(chs[5]["id"], "error", error="boom", error_stage="render")
    assert chs[5]["ord"] in [c["ord"] for c in db.pending(sid)]
    db.close()


def test_parse_range():
    from webnovel_audio.cli import _parse_range

    assert _parse_range("") == (None, None)
    assert _parse_range("5") == (5, 5)
    assert _parse_range("3-7") == (3, 7)
    assert _parse_range("4-") == (4, None)
    assert _parse_range("-6") == (None, 6)


def test_explicit_range_is_imperative(tmp_path, monkeypatch):
    """0.2: `render <slug> 2-3` renders 2-3 whatever their status — the job
    `series redo` used to do by rewriting DB state for a later sync."""
    from webnovel_audio import providers

    if not os.path.exists(FICTION_FIXTURE):
        return
    fiction_html = open(FICTION_FIXTURE, encoding="utf-8").read()
    monkeypatch.setattr(
        providers.RoyalRoadProvider, "raw",
        lambda self, url, *, cfg: fiction_html if "/chapter/" not in url else CHAPTER_STUB)
    monkeypatch.setattr(sync, "_cache_cover", lambda *a, **k: None)

    cfg = Config()
    cfg.royalroad.state_db = str(tmp_path / "state.db")
    cfg.royalroad.library_dir = str(tmp_path / "lib")
    cfg.synth.backend = "null"
    sync.add_series(cfg, "https://www.royalroad.com/fiction/424242/salvage-run",
                    start="3", log=lambda *_: None)          # 1-3 skipped

    db = DB(cfg.royalroad.state_db)
    s = db.get_series("salvage-run")
    assert [c["status"] for c in db.range(s["id"], 1, 3)] == ["skipped"] * 3
    db.close()

    # declarative: skipped chapters are left alone
    sync.run_stage(cfg, "rendered", "salvage-run", backend="null", log=lambda *_: None)
    db = DB(cfg.royalroad.state_db)
    s = db.get_series("salvage-run")
    assert [c["status"] for c in db.range(s["id"], 1, 3)] == ["skipped"] * 3
    assert all(c["status"] == "rendered" for c in db.range(s["id"], 4, 6))
    db.close()

    # imperative: an explicit range renders them regardless
    res = sync.run_stage(cfg, "rendered", "salvage-run", lo=1, hi=3, backend="null",
                         log=lambda *_: None)
    assert res.rendered == 3
    db = DB(cfg.royalroad.state_db)
    s = db.get_series("salvage-run")
    assert all(c["status"] == "rendered" for c in db.range(s["id"], 1, 3))
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
    assert len(db.pending(s["id"])) == 2                    # chapters 5, 6 (1-4 skipped)

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
    ch = next(e for e in events if e["event"] == "chapter" and e.get("result") == "ok")
    assert ch["number"] == 5 and "path" in ch and ch["audio_seconds"] >= 0

    db = DB(cfg.royalroad.state_db)
    s = db.get_series("424242")
    # 0.2: rendering does NOT move the reader's position — that conflation is
    # what used to hide un-rendered chapters behind the marker.
    assert s["progress_order"] == 3                         # unchanged by render
    done = [c for c in db.chapters(s["id"]) if c["status"] == "rendered"]
    assert len(done) == 1 and os.path.exists(done[0]["audio_path"])
    db.close()

    raw = tmp_path / "lib" / s["slug"] / ".raw"
    assert raw.is_dir() and any(raw.iterdir())              # chapter html cached


def test_series_add_registers_only(tmp_path, monkeypatch):
    """0.2: `add` is pure registration — no chapter downloads, no cast seeding."""
    from webnovel_audio import providers

    if not os.path.exists(FICTION_FIXTURE):
        return
    fiction_html = open(FICTION_FIXTURE, encoding="utf-8").read()
    hits = []

    def fake_raw(self, url, *, cfg):
        hits.append(url)
        return fiction_html if "/chapter/" not in url else CHAPTER_STUB

    monkeypatch.setattr(providers.RoyalRoadProvider, "raw", fake_raw)
    monkeypatch.setattr(sync, "_cache_cover", lambda *a, **k: None)

    cfg = Config()
    cfg.royalroad.state_db = str(tmp_path / "state.db")
    cfg.royalroad.library_dir = str(tmp_path / "lib")
    cfg.general.series_config_dir = str(tmp_path / "series")

    info = sync.add_series(cfg, "https://www.royalroad.com/fiction/424242/salvage-run",
                           start="2", log=lambda *_: None)
    assert info["chapters"] == 6
    assert not any("/chapter/" in u for u in hits)          # no chapter fetched
    assert not os.path.exists(os.path.join(cfg.general.series_config_dir,
                                           f"{info['slug']}.toml"))
    # "--from 2" is recorded per chapter, not as an invisible cutoff
    db = DB(cfg.royalroad.state_db)
    s = db.get_series("salvage-run")
    st = {c["ord"] + 1: c["status"] for c in db.chapters(s["id"])}
    assert st[1] == st[2] == "skipped" and st[3] == "new"
    assert [c["ord"] + 1 for c in db.pending(s["id"])] == [3, 4, 5, 6]
    db.close()


def test_append_cast_voices_inserts_into_existing_table():
    text = (
        '# a comment above\n\n[cast.voices]\n"Mara" = "af_heart"   # 8 line(s), female\n\n'
        "[chat]\nspeak_username = \"first\"\n"
    )
    out = sync._append_cast_voices(text, ['"Resk"           = "am_michael"   # 3 line(s), male'])
    assert '"Mara"' in out and '"Resk"' in out
    assert out.index('"Mara"') < out.index('"Resk"')          # existing entry untouched, kept first
    assert "[chat]" in out and "speak_username" in out         # unrelated section preserved verbatim
    assert out.index('"Resk"') < out.index("[chat]")           # inserted into the right table


def test_append_cast_voices_adds_table_if_missing():
    out = sync._append_cast_voices("[general]\nspeak_title = true\n", ['"Mara" = "af_heart"'])
    assert "[cast.voices]" in out and '"Mara"' in out
    assert out.index("[general]") < out.index("[cast.voices]")


def test_existing_cast_voice_keys():
    text = '[cast.voices]\n"Mara" = "af_heart"\nResk = "am_michael"\n'
    assert sync._existing_cast_voice_keys(text) == {"Mara", "Resk"}
    assert sync._existing_cast_voice_keys("not valid toml [[[") == set()


def test_suggest_cast_appends_new_speaker_on_a_later_range(tmp_path, monkeypatch):
    from webnovel_audio import providers

    if not os.path.exists(FICTION_FIXTURE):
        return
    fiction_html = open(FICTION_FIXTURE, encoding="utf-8").read()
    stub_a = CHAPTER_STUB                                       # chapters 1-3: "Resk"
    stub_b = CHAPTER_STUB.replace("Resk", "Mara")                # chapters 4-6: "Mara"

    def fake_raw(self, url, *, cfg):
        if "/chapter/" not in url:
            return fiction_html
        return stub_a if any(f"/{n}/" in url for n in (1001, 1002, 1003)) else stub_b

    monkeypatch.setattr(providers.RoyalRoadProvider, "raw", fake_raw)
    monkeypatch.setattr(sync, "_cache_cover", lambda *a, **k: None)

    cfg = Config()
    cfg.royalroad.state_db = str(tmp_path / "state.db")
    cfg.royalroad.library_dir = str(tmp_path / "lib")
    cfg.general.series_config_dir = str(tmp_path / "series")

    sync.add_series(cfg, "https://www.royalroad.com/fiction/424242/salvage-run",
                    start="start", log=lambda *_: None)

    r1 = sync.suggest_cast(cfg, "salvage-run", lo=1, hi=3, apply_cast=True,
                           log=lambda *_: None)
    assert r1["overlay_action"] == "created"
    assert set(r1["cast"]) == {"Resk"}

    r2 = sync.suggest_cast(cfg, "salvage-run", lo=4, hi=6, apply_cast=True,
                           log=lambda *_: None)
    assert r2["overlay_action"] == "appended"
    assert r2["cast"]["Mara"]["new"] and not r2["cast"].get("Resk", {}).get("new", False)

    text = open(r1["overlay_path"]).read()
    assert '"Resk"' in text and '"Mara"' in text
    import tomllib
    voices = tomllib.loads(text)["cast"]["voices"]
    assert voices["Resk"] == r1["cast"]["Resk"]["voice"]        # first pass's choice kept
    assert voices["Mara"] == r2["cast"]["Mara"]["voice"]

    r3 = sync.suggest_cast(cfg, "salvage-run", lo=1, hi=6, apply_cast=True,
                           log=lambda *_: None)
    assert r3["overlay_action"] == "unchanged"                   # both already mapped


def test_cmd_check_json_and_write(tmp_path, monkeypatch, capsys):
    import json
    import types

    from webnovel_audio import cli, providers

    if not os.path.exists(FICTION_FIXTURE):
        return
    fiction_html = open(FICTION_FIXTURE, encoding="utf-8").read()
    monkeypatch.setattr(
        providers.RoyalRoadProvider, "raw",
        lambda self, url, *, cfg: fiction_html if "/chapter/" not in url else CHAPTER_STUB,
    )
    monkeypatch.setattr(sync, "_cache_cover", lambda *a, **k: None)

    cfgp = tmp_path / "c.toml"
    cfgp.write_text(
        f'[royalroad]\nstate_db = "{tmp_path / "s.db"}"\nlibrary_dir = "{tmp_path / "lib"}"\n'
        f'[general]\nseries_config_dir = "{tmp_path / "series"}"\n'
        f'lexicon_dir = "{tmp_path / "lex"}"\n'
    )
    cfg = Config.load(str(cfgp))
    sync.add_series(cfg, "https://www.royalroad.com/fiction/424242/salvage-run",
                    start="start", log=lambda *_: None)

    rc = cli._cmd_check(types.SimpleNamespace(
        target="salvage-run", range="1-3", write=True, context=6,
        config=str(cfgp), json=True))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["slug"] == "salvage-run"
    assert "Resk" in out["cast"] and out["cast"]["Resk"]["new"]
    assert isinstance(out["heteronyms"], list) and isinstance(out["lexicon_candidates"], list)
    lex_path = tmp_path / "lex" / "salvage-run.csv"
    if out["lexicon_candidates"]:
        assert lex_path.exists()
        for name in out["lexicon_candidates"]:
            assert name in lex_path.read_text()


def test_suggest_voices_moved_to_dialogue():
    from webnovel_audio.dialogue import suggest_voices

    cfg = Config()
    out = suggest_voices({"Mara": 5, "Resk": 3}, {"Mara": "f", "Resk": "m"}, cfg)
    assert out["Mara"].startswith(("af_", "bf_")) and out["Resk"].startswith(("am_", "bm_"))
