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
    from webnovel_audio.cli import _parse_range, _span_bounds

    assert _parse_range("") == []
    assert _parse_range("5") == [(5, 5)]
    assert _parse_range("3-7") == [(3, 7)]
    assert _parse_range("4-") == [(4, None)]
    assert _parse_range("-6") == [(None, 6)]
    # comma lists: scattered UI selection -> one command
    assert _parse_range("1-3,7,20-25") == [(1, 3), (7, 7), (20, 25)]
    assert _parse_range(" 1 - 3 , 7 ") == [(1, 3), (7, 7)]

    assert _span_bounds([]) == (None, None)
    assert _span_bounds([(1, 3), (20, 25)]) == (1, 25)
    assert _span_bounds([(4, None)]) == (4, None)


def test_db_select_spans(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    sid = db.upsert_series(FictionInfo(rr_id="9", slug="demo", title="Demo",
                                       url="https://rr/9"))
    db.replace_chapters(sid, _chapters(10))
    nums = lambda spans: [c["ord"] + 1 for c in db.select(sid, spans)]

    assert nums(None) == list(range(1, 11))            # no spans -> everything
    assert nums([(2, 4)]) == [2, 3, 4]
    assert nums([(1, 2), (8, None)]) == [1, 2, 8, 9, 10]
    assert nums([(None, 2)]) == [1, 2]
    # overlapping spans must not duplicate, and order stays chapter order
    assert nums([(5, 7), (6, 8)]) == [5, 6, 7, 8]
    assert nums([(9, 10), (1, 1)]) == [1, 9, 10]
    # an explicit selection ignores status — even skipped/rendered come back
    db.set_status([c["id"] for c in db.chapters(sid)[:3]], "skipped")
    assert nums([(1, 3)]) == [1, 2, 3]
    db.close()


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


def test_skip_through_variants():
    chs = _chapters(10)
    assert sync._skip_through(chs, "start") == 0        # skip nothing
    assert sync._skip_through(chs, "latest") == 10      # all of them
    assert sync._skip_through(chs, "4") == 4            # read through #4
    assert sync._skip_through(chs, "https://rr/chapter/103/ch-4") == 4


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
    assert len(db.pending(s["id"])) == 2                    # chapters 5, 6 (1-4 skipped)

    summ = db.summary()[0]
    assert summ["slug"] == "salvage-run" and summ["provider"] == "royalroad"
    assert summ["pending"] == 2 and summ["stages"]["skipped"] == 4
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
    # ...but the resolved voices ARE pinned, so the series can't drift when the
    # global defaults change later. The overlay lives in the series bundle.
    overlay = os.path.join(cfg.royalroad.library_dir, info["slug"], "config.toml")
    assert os.path.exists(overlay)
    import tomllib
    pinned = tomllib.loads(open(overlay).read())
    assert pinned["voices"]["narrator"] == Config().voices.narrator
    assert pinned["cast"]["voices"] == {}                   # no speakers yet

    # and an empty but *named* lexicon, so an editor pane shows which series
    # is open the way config.toml already does
    lex = os.path.join(cfg.royalroad.library_dir, info["slug"], "lexicon.csv")
    assert os.path.exists(lex)
    assert open(lex).read().startswith(f"# {info['slug']} —")
    from webnovel_audio.lexicon import Lexicon
    assert Lexicon.load(lex).rules == []
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
    assert r1["overlay_action"] == "appended"      # into the pinned file from `add`
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


def test_cmd_check_is_report_only(tmp_path, monkeypatch, capsys):
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
        target="salvage-run", range="1-3", context=6, top=15,
        config=str(cfgp), json=True))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["slug"] == "salvage-run"
    assert "Resk" in out["cast"] and out["cast"]["Resk"]["new"]
    assert isinstance(out["heteronyms"], list) and isinstance(out["lexicon_candidates"], list)
    # `check` is report-only now: it must not have written the lexicon...
    assert not (tmp_path / "lex" / "salvage-run.csv").exists()
    # ...nor added anything to the cast
    assert out["overlay_action"] == "would-add"


def test_suggest_voices_moved_to_dialogue():
    from webnovel_audio.dialogue import suggest_voices

    cfg = Config()
    out = suggest_voices({"Mara": 5, "Resk": 3}, {"Mara": "f", "Resk": "m"}, cfg)
    assert out["Mara"].startswith(("af_", "bf_")) and out["Resk"].startswith(("am_", "bm_"))


def test_pin_defaults_text_is_valid_toml_and_records_resolved_voices():
    """`series add` pins the resolved voices so the series can't drift when the
    global defaults are retuned later (lockfile reasoning)."""
    import tomllib

    cfg = Config()
    cfg.voices.narrator = "bm_lewis"
    cfg.cast.default = "am_onyx"
    data = tomllib.loads(sync.pin_defaults_text(cfg, "demo"))
    assert data["voices"]["narrator"] == "bm_lewis"
    assert data["voices"]["thought"] == cfg.voices.thought
    assert data["cast"]["default"] == "am_onyx"
    assert data["cast"]["voices"] == {}
    assert data["chat"]["voices"] == cfg.chat.voices
    # timing/DSP deliberately not pinned — those are global taste
    assert "pauses" not in data and "dsp" not in data


def test_pinned_series_ignores_later_global_change(tmp_path):
    cfg = Config()
    cfg.general.series_config_dir = str(tmp_path)
    (tmp_path / "demo.toml").write_text(sync.pin_defaults_text(cfg, "demo"))
    original = cfg.voices.narrator

    cfg.voices.narrator = "bf_emma"                  # retune the global default
    assert sync._series_cfg(cfg, "demo").voices.narrator == original


def _sync_args(**kw):
    import types
    base = dict(key=None, limit=None, dry_run=False, backend="null", no_refresh=True,
                json=False, yes=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _seeded(tmp_path, n_rendered=2, n_new=3):
    """A library with some rendered chapters (for the median) and some outstanding."""
    cfg = Config()
    cfg.royalroad.state_db = str(tmp_path / "s.db")
    cfg.royalroad.library_dir = str(tmp_path / "lib")
    db = DB(cfg.royalroad.state_db)
    sid = db.upsert_series(FictionInfo(rr_id="9", slug="demo", title="Demo",
                                       url="https://rr/9"))
    db.replace_chapters(sid, _chapters(n_rendered + n_new))
    for c in db.chapters(sid)[:n_rendered]:
        db.mark(c["id"], "rendered", audio_path="/x.opus", duration_s=600.0)
    db.close()
    return cfg


def test_estimate_render_uses_series_median(tmp_path):
    cfg = _seeded(tmp_path)
    est = sync.estimate_render(cfg)
    assert est["chapters"] == 3
    # 3 chapters x 600 s median x RENDER_COST_RATIO
    assert est["seconds"] == pytest.approx(3 * 600.0 * sync.RENDER_COST_RATIO)
    assert est["series"][0]["from_samples"] == 2
    # limit caps the estimate the same way it caps the run
    assert sync.estimate_render(cfg, limit=1)["chapters"] == 1


def test_estimate_falls_back_without_samples(tmp_path):
    cfg = _seeded(tmp_path, n_rendered=0, n_new=2)
    est = sync.estimate_render(cfg)
    assert est["series"][0]["from_samples"] == 0
    assert est["seconds"] == pytest.approx(2 * sync._FALLBACK_CHAPTER_SECONDS
                                           * sync.RENDER_COST_RATIO)


def test_human_duration():
    assert sync.human_duration(45) == "45s"
    assert sync.human_duration(600) == "10m"
    assert sync.human_duration(3600 * 2 + 300) == "2h 05m"


def test_sync_confirmation_declined(tmp_path, monkeypatch, capsys):
    from webnovel_audio import cli

    cfg = _seeded(tmp_path)
    cfgp = tmp_path / "c.toml"
    cfgp.write_text(f'[royalroad]\nstate_db = "{cfg.royalroad.state_db}"\n'
                    f'library_dir = "{cfg.royalroad.library_dir}"\n')
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    called = []
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: called.append(1))

    rc = cli._cmd_sync(_sync_args(config=str(cfgp)))
    out = capsys.readouterr().out
    assert rc == 0 and not called                   # declined -> nothing ran
    assert "3 chapter(s) to render" in out and "nothing done." in out


def test_sync_yes_flag_skips_prompt(tmp_path, monkeypatch):
    from webnovel_audio import cli

    cfg = _seeded(tmp_path)
    cfgp = tmp_path / "c.toml"
    cfgp.write_text(f'[royalroad]\nstate_db = "{cfg.royalroad.state_db}"\n'
                    f'library_dir = "{cfg.royalroad.library_dir}"\n')

    def boom(*_a, **_k):
        raise AssertionError("must not prompt with --yes")

    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: sync.SyncResult())
    assert cli._cmd_sync(_sync_args(config=str(cfgp), yes=True)) == 0
    # ...and a non-tty (cron / systemd timer) must not prompt either
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli._cmd_sync(_sync_args(config=str(cfgp))) == 0


def test_opus_tags_carry_provenance(tmp_path):
    """Series + chapter provenance for the Opus stream. Verified against real
    ffmpeg output that custom Vorbis keys survive libopus."""
    import json as _json

    cfg = Config()
    db = DB(str(tmp_path / "s.db"))
    fi = FictionInfo(rr_id="9", slug="demo", title="Demo", author="A Writer",
                     url="https://rr/9", tags=["Sci-fi", "Space Opera"],
                     warnings=["Graphic Violence", "Profanity"],
                     status="ONGOING", rating=4.5)
    sid = db.upsert_series(fi)
    db.replace_chapters(sid, _chapters(2))
    s, c = db.get_series("demo"), db.chapters(sid)[1]

    tags = sync._opus_tags(cfg, s, c)
    assert tags["TRACKNUMBER"] == "2"
    assert tags["DATE"] == "2026-01-01"
    assert tags["PERFORMER"] == cfg.voices.narrator
    assert tags["CONTENT_WARNING"] == "Graphic Violence; Profanity"
    assert tags["KEYWORDS"] == "Sci-fi; Space Opera"
    assert "RENDERED_AT" in tags and "ORGANIZATION" in tags
    assert _json.loads(s["warnings"]) == ["Graphic Violence", "Profanity"]
    assert s["status"] == "ONGOING" and s["rating"] == 4.5

    # a series with no metadata (or corrupt JSON) must still produce valid tags
    db.con.execute("UPDATE series SET tags='{bad', warnings=NULL WHERE id=?", (sid,))
    db.con.commit()
    tags = sync._opus_tags(cfg, db.get_series("demo"), c)
    assert "CONTENT_WARNING" not in tags and "KEYWORDS" not in tags
    assert tags["TRACKNUMBER"] == "2"
    db.close()


def test_stage_never_demotes_a_finished_chapter(tmp_path):
    """Re-running an earlier stage on a rendered chapter must not move it back.
    `parse <slug> 2-3` on already-rendered chapters used to reset them to
    'parsed', which then made them look outstanding again."""
    db = DB(str(tmp_path / "s.db"))
    sid = db.upsert_series(FictionInfo(rr_id="9", slug="demo", title="Demo",
                                       url="https://rr/9"))
    db.replace_chapters(sid, _chapters(3))
    c = db.chapters(sid)[0]

    db.mark_stage(c["id"], "rendered", audio_path="/x.opus", duration_s=500.0)
    assert db.chapters(sid)[0]["status"] == "rendered"

    db.mark_stage(c["id"], "parsed", text_path="/x.md")
    row = db.chapters(sid)[0]
    assert row["status"] == "rendered"          # not demoted
    assert row["text_path"] == "/x.md"          # but the re-run IS recorded
    assert row["parsed_at"]                     # ...including its timestamp
    assert row["audio_path"] == "/x.opus"       # and earlier work is intact

    db.mark_stage(c["id"], "fetched", raw_path="/x.html")
    assert db.chapters(sid)[0]["status"] == "rendered"
    # a fresh chapter still advances normally
    c2 = db.chapters(sid)[1]
    db.mark_stage(c2["id"], "fetched", raw_path="/y.html")
    assert db.chapters(sid)[1]["status"] == "fetched"
    db.close()


def _vols():
    from webnovel_audio.royalroad import VolumeRef
    # RR order is NOT contiguous — Sky Pride really runs 1,2,3,4,6,7
    return [VolumeRef(rr_id="10", title="Vol One", cover_url="", order=1),
            VolumeRef(rr_id="20", title="Vol Two", cover_url="", order=3)]


def test_volumes_and_positions(tmp_path):
    """Volume membership is per chapter and optional; position within a volume
    is computed, and is NOT the author's chapter number (an interstitial
    shifts it — Sky Pride V2 has an author's note at position 11)."""
    from webnovel_audio.royalroad import ChapterRef

    db = DB(str(tmp_path / "s.db"))
    fi = FictionInfo(rr_id="9", slug="demo", title="Demo", url="https://rr/9",
                     volumes=_vols())
    sid = db.upsert_series(fi)
    db.replace_volumes(sid, fi.volumes)

    chs = [
        ChapterRef("1", 0, "V1 C1", "a", "u1", "", True, volume_id="10"),
        ChapterRef("2", 1, "V1 C2", "b", "u2", "", True, volume_id="10"),
        ChapterRef("3", 2, "announcement", "c", "u3", "", True, volume_id=""),  # orphan
        ChapterRef("4", 3, "V2 C1", "d", "u4", "", True, volume_id="20"),
        ChapterRef("5", 4, "a note", "e", "u5", "", True, volume_id="20"),
        ChapterRef("6", 5, "V2 C2", "f", "u6", "", True, volume_id="20"),
    ]
    db.replace_chapters(sid, chs)
    rows = {c["ord"] + 1: c for c in db.chapters(sid)}

    assert rows[1]["volume_rr_id"] == "10" and rows[1]["volume_chapter"] == 1
    assert rows[2]["volume_chapter"] == 2
    assert rows[3]["volume_rr_id"] is None and rows[3]["volume_chapter"] is None
    assert rows[4]["volume_chapter"] == 1          # position restarts per volume
    # the interstitial at position 2 pushes "V2 C2" to position 3
    assert rows[5]["volume_chapter"] == 2
    assert rows[6]["volume_chapter"] == 3

    vm = db.volume_map(sid)
    assert vm["10"]["index"] == 1 and vm["20"]["index"] == 2   # contiguous...
    assert vm["20"]["row"]["ord"] == 3                          # ...unlike RR's order
    db.close()


def test_opus_tags_use_volume(tmp_path):
    from webnovel_audio.royalroad import ChapterRef

    cfg = Config()
    db = DB(str(tmp_path / "s.db"))
    fi = FictionInfo(rr_id="9", slug="demo", title="Demo", author="A", url="https://rr/9",
                     volumes=_vols())
    sid = db.upsert_series(fi)
    db.replace_volumes(sid, fi.volumes)
    db.replace_chapters(sid, [
        ChapterRef("4", 3, "V2 C1", "d", "u4", "2025-01-02T00:00:00Z", True, volume_id="20"),
    ])
    c = db.chapters(sid)[0]
    vm = db.volume_map(sid)
    t = sync._opus_tags(cfg, db.get_series("demo"), c, vm["20"], rendered_at="2026-01-01T00:00:00")
    assert t["TRACKNUMBER"] == "1"                  # volume-relative, not global 4
    assert t["VOLUME"] == "Vol Two" and t["VOLUME_INDEX"] == "2"
    assert t["album"] == "Demo — Vol Two"
    assert t["RENDERED_AT"] == "2026-01-01T00:00:00"   # never "now"
    db.close()


def test_cast_set_updates_in_place(tmp_path):
    """`cast set` is the agent-facing counterpart to `cast edit`: it must change
    exactly one row and leave everything else byte-identical."""
    cfg = Config()
    cfg.general.series_config_dir = str(tmp_path)
    p = tmp_path / "demo.toml"
    p.write_text(sync.pin_defaults_text(cfg, "demo"))

    r = sync.set_cast_voice(cfg, "demo", "Mara", "af_heart")
    assert r["action"] == "added"
    import tomllib
    assert tomllib.loads(p.read_text())["cast"]["voices"] == {"Mara": "af_heart"}

    before = p.read_text()
    r = sync.set_cast_voice(cfg, "demo", "Mara", "bf_emma")
    assert r["action"] == "updated"
    after = p.read_text()
    assert tomllib.loads(after)["cast"]["voices"] == {"Mara": "bf_emma"}
    # only the one line changed
    diff = [a for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert len(diff) == 1 and "Mara" in diff[0]

    # an existing trailing comment survives an update
    lines = p.read_text().splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.startswith('"Mara"'))
    lines[i] += "   # 8 line(s), female"
    p.write_text("\n".join(lines) + "\n")
    sync.set_cast_voice(cfg, "demo", "Mara", "af_sky")
    assert "8 line(s), female" in p.read_text()
    assert tomllib.loads(p.read_text())["cast"]["voices"]["Mara"] == "af_sky"

    # empty voice = listed but unassigned
    sync.set_cast_voice(cfg, "demo", "girl", "")
    assert tomllib.loads(p.read_text())["cast"]["voices"]["girl"] == ""


def test_cli_structured_errors(tmp_path, capsys):
    """An agent branches on `code`, not on English prose."""
    import json
    import types

    from webnovel_audio import cli

    cfgp = tmp_path / "c.toml"
    cfgp.write_text(f'[royalroad]\nstate_db = "{tmp_path / "s.db"}"\n')

    rc = cli._cmd_cast(types.SimpleNamespace(
        action="set", key="nope", speaker="X", voice="am_michael",
        config=str(cfgp), json=True))
    err = json.loads(capsys.readouterr().out)["error"]
    assert rc == 1 and err["code"] == "no_such_series" and err["hint"]

    rc = cli._cmd_state(types.SimpleNamespace(
        action="set", key="nope", range="1", status="new",
        config=str(cfgp), json=True))
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "no_such_series"


def test_schema_covers_every_command(capsys):
    """The schema is walked out of argparse, so it can't drift from the parser."""
    import json
    import types

    from webnovel_audio import cli

    cli._cmd_schema(types.SimpleNamespace())
    d = json.loads(capsys.readouterr().out)
    paths = {" ".join(n["path"]) for n in d["detail"]}
    for expect in ("fetch", "parse", "check", "render", "sync", "cast set",
                   "lex add", "state set", "series disable", "schema"):
        assert expect in paths, expect
    assert set(d["commands"]) <= paths
    # conventions an agent needs in order to plan
    for k in ("target", "range", "json", "exit_codes", "errors"):
        assert k in d["conventions"]
    cast_set = next(n for n in d["detail"] if n["path"] == ["cast", "set"])
    assert [p["name"] for p in cast_set["positional"]] == ["key", "speaker", "voice"]
    assert cast_set["help"]


def _series(db, slug, title):
    return db.upsert_series(FictionInfo(rr_id=slug, slug=slug, title=title,
                                        author="A", url=f"https://rr/{slug}"))


def test_priority_orders_every_consumer_the_same_way(tmp_path):
    """One ordering drives `sync`, the CLI and the UI, because they all go
    through list_series. If they drifted apart, "what renders first" and
    "what is at the top of the screen" would stop agreeing."""
    db = DB(str(tmp_path / "s.db"))
    low = _series(db, "low", "Aaa Low")        # alphabetically first
    high = _series(db, "high", "Zzz High")     # alphabetically last

    # defaults: nothing set, so it falls back to title order
    assert [s["slug"] for s in db.list_series()] == ["low", "high"]

    db.set_priority(high, 900)
    assert [s["slug"] for s in db.list_series()] == ["high", "low"]


def test_pausing_preserves_priority(tmp_path):
    """The reason priority is a separate column from `enabled`.

    Overloading one integer -- 0 meaning paused -- would have to destroy the
    priority to pause, leaving nothing to restore on resume.
    """
    db = DB(str(tmp_path / "s.db"))
    a = _series(db, "a", "A")
    _series(db, "b", "B")
    db.set_priority(a, 900)

    db.set_enabled(a, False)
    rows = db.list_series()
    assert [s["slug"] for s in rows] == ["b", "a"], "paused sorts last"
    assert dict(rows[1])["priority"] == 900, "but keeps its priority"

    db.set_enabled(a, True)
    assert [s["slug"] for s in db.list_series()] == ["a", "b"], "resume restores it"


def test_priority_column_is_added_to_an_existing_db(tmp_path):
    """Upgrading a tracked library must not need a re-import."""
    import sqlite3

    path = str(tmp_path / "old.db")
    db = DB(path)
    sid = _series(db, "x", "X")
    db.close()

    # simulate the pre-priority schema
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE series DROP COLUMN priority")
    con.commit()
    assert "priority" not in {r[1] for r in con.execute("PRAGMA table_info(series)")}
    con.close()

    db = DB(path)                                   # migrates on open
    assert "priority" in {r["name"] for r in
                          db.con.execute("PRAGMA table_info(series)")}
    assert dict(db.list_series()[0])["priority"] == 100, "existing rows get the default"
    db.set_priority(sid, 300)
    assert dict(db.list_series()[0])["priority"] == 300
