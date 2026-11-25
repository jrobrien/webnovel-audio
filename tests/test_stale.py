"""`stale`: which artifacts are out of date, why, and what fixes them. Read-only."""
import hashlib
import json
import os
import shutil
import sqlite3

import pytest

from webnovel_audio import cli, pipeline, stale, sync
from webnovel_audio.config import Config
from webnovel_audio.db import DB

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "samples", "salvage-run-fiction.html")
pytestmark = [pytest.mark.skipif(not os.path.exists(FIXTURE), reason="needs the fixture"),
              pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")]

PAGE = """<html><head><meta property="og:title" content="{title} - Salvage Run">
<meta property="og:url" content="{url}">
</head><body><h4>by <a href="/profile/7">Jo Writer</a></h4><div class="chapter-content">
{body}
</div></body></html>"""

#: odd chapters mention Kaelith (no lexicon has it) and have Resk speak; even ones have Mira speak
REBAR = '<p>He picked up the Kaelith bar and swung it at the door.</p><p>"Careful," Resk said.</p>'
PLAIN = '<p>The corridor ended at a sealed door.</p><p>"Run," Mira said.</p>'


@pytest.fixture
def lib(tmp_path, monkeypatch):
    from webnovel_audio import providers

    fiction = open(FIXTURE, encoding="utf-8").read()
    monkeypatch.setattr(providers.RoyalRoadProvider, "_get",
                        lambda self, url, ctx: fiction)
    monkeypatch.setattr(sync, "_cache_cover", lambda *a, **k: None)
    cfg = Config()
    cfg.library.state_db = str(tmp_path / "state.db")
    cfg.library.library_dir = str(tmp_path / "lib")
    cfg.general.cache_dir = str(tmp_path / "cache")
    cfg.synth.backend = "null"
    cfg.audio.master_jobs = 1
    sync.add_series(cfg, "https://www.royalroad.com/fiction/424242/salvage-run",
                    start="1", log=lambda *_: None)
    db = DB(cfg.library.state_db)
    s = db.get_series("salvage-run")
    for c in db.chapters(s["id"]):
        if c["status"] != "new":
            continue
        body = REBAR if (c["ord"] + 1) % 2 else PLAIN
        sync.ingest_raw(cfg, db, PAGE.format(title=c["title"], url=c["url"], body=body))
    db.close()
    sync.run_sync(cfg, backend="null", log=lambda *_: None, refresh_first=False)

    def run(**kw):
        kw.setdefault("jobs", 1)
        return stale.report(cfg, "salvage-run", **kw)[0]

    run.cfg = cfg
    run.bdir = str(tmp_path / "lib" / "salvage-run")
    db = DB(cfg.library.state_db)
    run.rendered = [c["ord"] + 1 for c in db.chapters(db.get_series("salvage-run")["id"])
                    if c["status"] == "rendered"]
    db.close()
    assert len(run.rendered) >= 4
    return run


def _audio(r):
    return {c["number"]: c["stale"]["audio"] for c in r["chapters"] if "audio" in c["stale"]}


def test_a_fresh_library_reports_nothing(lib):
    r = lib(tags=True)
    assert r["chapters"] == [] and r["summary"] == {} and r["problems"] == []
    assert r["checked"]["audio"] == len(lib.rendered)


def test_a_lexicon_row_stales_only_the_chapters_it_hits(lib):
    with open(os.path.join(lib.bdir, "lexicon.csv"), "a", encoding="utf-8") as fh:
        fh.write("Kaelith,,kay-lith\n")
    r = lib(why=True)
    audio = _audio(r)
    odd = [n for n in lib.rendered if n % 2]
    assert sorted(audio) == odd
    assert all(v == ["text/lexicon"] for v in audio.values())
    first = next(c for c in r["chapters"] if c["number"] == odd[0])
    assert any('"the Kaelith bar"' in d and '"the Kay-lith bar"' in d for d in first["diff"]), \
        first["diff"]
    summ = r["summary"]["audio"]
    assert summ["count"] == len(odd)
    assert summ["fix"] == ("webnovel-audio render --target salvage-run --range "
                           + stale.compact_range(odd))


def test_a_cast_change_stales_only_chapters_where_that_speaker_talks(lib):
    sync.set_cast_voice(lib.cfg, "salvage-run", "Mira", "af_nova", lib.bdir)
    r = lib(why=True)
    audio = _audio(r)
    assert sorted(audio) == [n for n in lib.rendered if n % 2 == 0]
    assert all(v == ["cast/voice"] for v in audio.values())
    diff = r["chapters"][0]["diff"]
    assert any(d.startswith("voice Mira:") and d.endswith("→ af_nova (1 segment)")
               for d in diff), diff
    assert "cast/voice" in stale.render_text([r])


def test_an_audio_setting_change_is_reason_audio(lib):
    with open(os.path.join(lib.bdir, "config.toml"), "a", encoding="utf-8") as fh:
        fh.write('\n[audio]\nopus_bitrate = "40k"\n')
    assert set(map(tuple, _audio(lib()).values())) == {("audio",)}
    assert sorted(_audio(lib())) == lib.rendered


def test_a_dsp_change_is_reason_dsp(lib):
    with open(os.path.join(lib.bdir, "config.toml"), "a", encoding="utf-8") as fh:
        fh.write("\n[dsp.narration]\nrobot_hz = 50\nrobot_mix = 0.3\n")
    assert set(map(tuple, _audio(lib()).values())) == {("dsp",)}


def _recipes(lib):
    return sorted(os.path.join(lib.bdir, "chapters", f)
                  for f in os.listdir(os.path.join(lib.bdir, "chapters"))
                  if f.endswith(".recipe"))


def test_a_one_line_recipe_still_matches(lib):
    for p in _recipes(lib):
        first = open(p, encoding="utf-8").read().splitlines()[0]
        open(p, "w", encoding="utf-8").write(first + "\n")       # what an older render wrote
        assert pipeline._recipe_matches(p[:-len(".recipe")] + ".opus", first)
    assert lib()["chapters"] == []
    # and a stale one says it cannot tell why
    with open(os.path.join(lib.bdir, "config.toml"), "a", encoding="utf-8") as fh:
        fh.write('\n[audio]\nopus_bitrate = "40k"\n')
    assert all(v[0] == stale.NO_PARTS for v in _audio(lib()).values())


def test_a_missing_recipe_is_unknown(lib):
    os.remove(_recipes(lib)[0])
    assert list(_audio(lib()).values()) == [["unknown"]]


def test_a_corrected_author_stales_text_and_tags(lib):
    con = sqlite3.connect(lib.cfg.library.state_db)
    con.execute("UPDATE series SET author='Someone Else'")
    con.commit()
    con.close()
    r = lib(tags=True)
    assert all(c["stale"]["text"] == ["front matter: author"] for c in r["chapters"])
    assert all(c["stale"]["tags"] == ["artist"] for c in r["chapters"])
    assert {c["number"] for c in r["chapters"]} == set(lib.rendered)
    assert r["summary"]["tags"]["fix"] == "webnovel-audio retag --scope salvage-run"
    assert r["summary"]["text"]["fix"].startswith("webnovel-audio parse --target salvage-run")
    assert "tags" not in lib()["summary"]                  # only checked with --tags


def test_episodes_member_rerendered_and_orphan(lib):
    from webnovel_audio import episodes

    path = os.path.join(lib.bdir, "config.toml")
    text = open(path, encoding="utf-8").read()
    assert "# chapters_per_episode = 5" in text               # the pinned [feed] section
    open(path, "w", encoding="utf-8").write(
        text.replace("# chapters_per_episode = 5", "chapters_per_episode = 2"))
    db = DB(lib.cfg.library.state_db)
    s = db.get_series("salvage-run")
    planned = episodes.plan(db.chapters(s["id"]), 2)
    assert lib()["summary"]["episodes"]["reasons"] == {"missing": len(planned)}
    episodes.build(lib.cfg, db, s, log=lambda *_: None)
    db.close()
    assert "episodes" not in lib()["summary"]
    os.utime(planned[0].first["audio_path"], ns=(1, 1))     # a re-rendered chapter
    open(os.path.join(lib.bdir, "episodes", "e0099-0100.opus"), "wb").close()
    r = lib()
    assert {e["episode"]: e["reasons"] for e in r["episodes"]["stale"]} == {
        planned[0].name: ["members changed"], "e0099-0100.opus": ["orphan"]}
    assert r["summary"]["episodes"]["fix"] == "webnovel-audio episodes --scope salvage-run"


def _snapshot(lib):
    files = {}
    for root, _, names in os.walk(os.path.dirname(lib.bdir)):
        for n in names:
            p = os.path.join(root, n)
            st = os.stat(p)
            files[p] = (st.st_mtime_ns, st.st_size)
    db = open(lib.cfg.library.state_db, "rb").read()
    return files, hashlib.sha256(db).hexdigest()


def test_it_never_writes_anything(lib, capsys, tmp_path):
    with open(os.path.join(lib.bdir, "lexicon.csv"), "a", encoding="utf-8") as fh:
        fh.write("Kaelith,,kay-lith\n")
    os.remove(_recipes(lib)[0])
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'[library]\nstate_db = "{lib.cfg.library.state_db}"\n'
                        f'library_dir = "{os.path.dirname(lib.bdir)}"\n'
                        f'[general]\ncache_dir = "{tmp_path / "cache"}"\n'
                        '[synth]\nbackend = "null"\n')
    before = _snapshot(lib)
    lib(tags=True, why=True)
    lib(tags=True, why=True, jobs=2)
    assert cli.main(["stale", "-c", str(cfg_path), "--tags", "--why"]) == 0
    assert cli.main(["stale", "-c", str(cfg_path), "--scope", "salvage-run", "--json"]) == 0
    assert _snapshot(lib) == before


def test_parallel_matches_serial(lib):
    with open(os.path.join(lib.bdir, "lexicon.csv"), "a", encoding="utf-8") as fh:
        fh.write("Kaelith,,kay-lith\n")
    assert lib(why=True, jobs=2) == lib(why=True, jobs=1)


def test_json_shape_and_range(lib, capsys, tmp_path):
    with open(os.path.join(lib.bdir, "lexicon.csv"), "a", encoding="utf-8") as fh:
        fh.write("Kaelith,,kay-lith\n")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'[library]\nstate_db = "{lib.cfg.library.state_db}"\n'
                        f'library_dir = "{os.path.dirname(lib.bdir)}"\n'
                        '[synth]\nbackend = "null"\n')
    capsys.readouterr()
    assert cli.main(["stale", "-c", str(cfg_path), "--scope", "salvage-run",
                     "--range", "2-3", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and len(out["series"]) == 1
    r = out["series"][0]
    assert {"slug", "title", "checked", "chapters", "episodes", "problems", "summary"} <= set(r)
    assert r["checked"]["text"] == 2
    assert [c["number"] for c in r["chapters"]] == [3]
    assert r["chapters"][0]["stale"] == {"audio": ["text/lexicon"]}
    assert r["summary"]["audio"]["range"] == "3"
    assert cli.main(["stale", "-c", str(cfg_path), "--scope", "nope", "--json"]) == 1


def test_compact_range():
    assert stale.compact_range([12, 40, 41, 42, 43, 44]) == "12,40-44"
    assert stale.compact_range([3]) == "3" and stale.compact_range([]) == ""


def test_segment_diff_groups_voice_changes():
    old = [{"text": "Hi.", "voice": "af_bella", "speaker": "Mira", "style": "dialogue",
            "rate": 1.0, "pitch": 0.0, "kind": "speech", "pause_after_ms": 0}] * 3
    new = [dict(d, voice="af_nova") for d in old]
    assert stale.segment_diff(old, new) == ["voice Mira: af_bella → af_nova (3 segments)"]
    assert stale.seg_changes(old, new) == ["cast/voice"]
