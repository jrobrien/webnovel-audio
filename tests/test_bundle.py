"""Bundle export/import, and the round-trip that has to hold before the file
moves in plan step 3 can be trusted."""
import json
import os
import tomllib

import pytest

from webnovel_audio import bundle
from webnovel_audio.config import Config
from webnovel_audio.db import DB


class _Fic:
    """Minimal stand-in for a provider's series info."""
    def __init__(self, rr_id="999", slug="test-series", n=4):
        self.rr_id, self.slug = rr_id, slug
        self.title, self.author = "Test Series", "An Author"
        self.url = f"https://example.test/fiction/{rr_id}"
        self.cover_url, self.provider = "", "royalroad"
        self.tags, self.warnings = ["Fantasy", "Litrpg"], ["Gore"]
        self.status, self.rating = "ONGOING", 4.5
        self.volumes = [_Vol("10", "Volume One", 1), _Vol("11", "Volume Two", 2)]
        self.chapters = [_Ch(rr_id, i) for i in range(n)]


class _Vol:
    def __init__(self, rr_id, title, order):
        self.rr_id, self.title, self.order = rr_id, title, order
        self.cover_url = f"https://example.test/cover-{rr_id}.jpg"


class _Ch:
    def __init__(self, sid, i):
        self.rr_id, self.order = f"{sid}-{i}", i
        self.title, self.slug = f"Chapter {i + 1}", f"chapter-{i + 1}"
        self.url = f"https://example.test/ch/{i}"
        self.published_at, self.unlocked = "2026-01-01T00:00:00", True
        self.volume_id = "10" if i < 2 else "11"


@pytest.fixture
def lib(tmp_path):
    """A config + DB + one populated series, laid out as today's library."""
    cfg = Config()
    cfg.royalroad.state_db = str(tmp_path / "state.db")
    cfg.royalroad.library_dir = str(tmp_path / "library")
    cfg.general.base_lexicon = str(tmp_path / "_base.csv")
    (tmp_path / "_base.csv").write_text("surface,respell,ipa,notes\nfoo,foo,,\n")

    db = DB(cfg.royalroad.state_db)
    fi = _Fic()
    sid = db.upsert_series(fi)
    db.replace_volumes(sid, fi.volumes)
    db.replace_chapters(sid, fi.chapters)

    d = os.path.join(cfg.royalroad.library_dir, fi.slug)
    os.makedirs(d, exist_ok=True)
    # give one chapter a full rendered history, including the timings that
    # file-presence inference could never recover
    audio = os.path.join(d, "001-chapter-1.opus")
    open(audio, "wb").close()
    c = db.chapters(sid)[0]
    db.con.execute(
        """UPDATE chapters SET status='rendered', audio_path=?, duration_s=931.5,
           rendered_at='2026-09-10T04:00:00', render_started_at='2026-09-10T03:57:00',
           render_ended_at='2026-09-10T04:00:00', narrator='am_michael',
           fetched_at='2026-09-09T01:00:00', parsed_at='2026-09-09T01:01:00'
           WHERE id=?""", (audio, c["id"]))
    db.con.commit()
    yield cfg, db, db.get_series("999"), d
    db.close()


def test_export_writes_both_machine_files(lib):
    cfg, db, s, d = lib
    res = bundle.sync_bundle(cfg, db, s)
    assert res["ok"]

    with open(os.path.join(d, bundle.MANIFEST), "rb") as fh:
        man = tomllib.load(fh)
    assert man["schema"] == bundle.SCHEMA
    assert man["uuid"] and man["slug"] == "test-series"
    assert man["source"]["id"] == "999"
    assert man["source"]["url"].endswith("/fiction/999")
    assert man["source"]["provider"] == "royalroad"
    assert len(man["provenance"]["base_lexicon_sha256"]) == 64

    st = json.loads((open(os.path.join(d, bundle.STATE))).read())
    assert st["schema"] == bundle.SCHEMA
    assert len(st["chapters"]) == 4 and len(st["volumes"]) == 2
    # paths are stored bundle-relative so the directory can be moved
    assert st["chapters"][0]["audio_path"] == "001-chapter-1.opus"


def test_export_records_the_path_and_uuid_on_the_row(lib):
    cfg, db, s, d = lib
    assert s["path"] is None
    bundle.sync_bundle(cfg, db, s)
    again = db.get_series("999")
    assert again["path"] == os.path.abspath(d)
    assert again["uuid"] == s["uuid"]           # identity is stable, not regenerated


def _snapshot(db, sid):
    cols = list(bundle._CHAPTER_COLS)
    return {
        "chapters": [tuple(c[k] for k in cols) for c in db.chapters(sid)],
        "volumes": [tuple(v[k] for k in bundle._VOLUME_COLS) for v in db.volumes(sid)],
        "series": {k: db.get_series("999")[k] for k in bundle._SERIES_COLS},
    }


def test_round_trip_into_a_fresh_db_is_exact(lib, tmp_path):
    """The claim plan step 2 rests on: a bundle can rebuild the DB rows.

    Specifically covers `render_started_at`/`render_ended_at`, which inference
    from files on disk could not recover — they are the render-cost model's
    only input.
    """
    cfg, db, s, d = lib
    bundle.sync_bundle(cfg, db, s)
    before = _snapshot(db, s["id"])

    fresh = DB(str(tmp_path / "fresh.db"))
    res = bundle.import_bundle(cfg, fresh, d)
    assert res["action"] == "add" and res["chapters"] == 4

    new = fresh.get_series("999")
    assert new["uuid"] == s["uuid"]             # identity survives the hop
    after = _snapshot(fresh, new["id"])

    assert after["volumes"] == before["volumes"]
    assert after["series"] == before["series"]
    assert after["chapters"] == before["chapters"]
    ch = fresh.chapters(new["id"])[0]
    assert ch["render_started_at"] == "2026-09-10T03:57:00"
    assert ch["render_ended_at"] == "2026-09-10T04:00:00"
    assert ch["duration_s"] == 931.5
    fresh.close()


def test_import_is_idempotent_and_matches_by_uuid(lib, tmp_path):
    cfg, db, s, d = lib
    bundle.sync_bundle(cfg, db, s)
    fresh = DB(str(tmp_path / "fresh.db"))
    assert bundle.import_bundle(cfg, fresh, d)["action"] == "add"
    assert bundle.import_bundle(cfg, fresh, d)["action"] == "update"
    assert len(fresh.list_series()) == 1
    assert len(fresh.chapters(fresh.get_series("999")["id"])) == 4
    fresh.close()


def test_import_into_the_same_db_updates_rather_than_duplicates(lib):
    cfg, db, s, d = lib
    bundle.sync_bundle(cfg, db, s)
    res = bundle.import_bundle(cfg, db, d)
    assert res["action"] == "update"
    assert len(db.list_series()) == 1


def test_relocating_a_bundle_rewrites_absolute_paths(lib, tmp_path):
    """`mv` the directory; import must re-anchor the stored file paths."""
    cfg, db, s, d = lib
    bundle.sync_bundle(cfg, db, s)
    moved = str(tmp_path / "elsewhere" / "test-series")
    os.makedirs(os.path.dirname(moved), exist_ok=True)
    os.rename(d, moved)

    fresh = DB(str(tmp_path / "fresh.db"))
    bundle.import_bundle(cfg, fresh, moved)
    row = fresh.get_series("999")
    assert row["path"] == os.path.abspath(moved)
    ch = fresh.chapters(row["id"])[0]
    assert ch["audio_path"] == os.path.join(moved, "001-chapter-1.opus")
    assert os.path.isfile(ch["audio_path"])
    fresh.close()


def test_bundle_dir_prefers_the_recorded_path(lib, tmp_path):
    """Everything resolves through series.path, so plan step 3 only has to
    change where the files are — not the callers."""
    cfg, db, s, d = lib
    assert bundle.bundle_dir(cfg, s) == os.path.abspath(d)     # falls back to library_dir
    db.set_bundle(s["id"], "/somewhere/else", s["uuid"])
    assert bundle.bundle_dir(cfg, db.get_series("999")) == "/somewhere/else"


def test_missing_bundle_is_a_supported_state(lib):
    """`rm -rf` on a series directory is a legitimate way to reclaim space;
    nothing may crash, and it must never be inferred as a deletion."""
    import shutil
    cfg, db, s, d = lib
    bundle.sync_bundle(cfg, db, s)
    shutil.rmtree(d)

    s = db.get_series("999")
    assert bundle.status(cfg, s) == "missing"
    assert not bundle.exists(cfg, s)
    assert bundle.sync_bundle(cfg, db, s) == {"ok": False, "reason": "missing",
                                              "path": os.path.abspath(d)}
    # still tracked: removal stays explicit
    assert db.get_series("999") is not None
    results = bundle.scan(cfg, db, cfg.royalroad.library_dir)
    assert [r["action"] for r in results] == ["missing"]


def test_bare_directory_without_a_manifest(lib):
    cfg, db, s, d = lib
    assert bundle.status(cfg, s) == "bare"
    with pytest.raises(bundle.BundleError) as exc:
        bundle.import_bundle(cfg, db, d)
    assert exc.value.code == "not_a_bundle"


def test_scan_imports_and_relocates(lib, tmp_path):
    cfg, db, s, d = lib
    bundle.sync_bundle(cfg, db, s)
    fresh = DB(str(tmp_path / "fresh.db"))
    out = bundle.scan(cfg, fresh, cfg.royalroad.library_dir)
    assert [r["action"] for r in out] == ["add"]
    assert fresh.get_series("999")["path"] == os.path.abspath(d)
    fresh.close()


def test_scan_dry_run_writes_nothing(lib, tmp_path):
    cfg, db, s, d = lib
    bundle.sync_bundle(cfg, db, s)
    fresh = DB(str(tmp_path / "fresh.db"))
    out = bundle.scan(cfg, fresh, cfg.royalroad.library_dir, dry_run=True)
    assert out[0]["dry_run"] and out[0]["action"] == "add"
    assert fresh.list_series() == []
    fresh.close()


def test_future_schema_is_refused(lib):
    cfg, db, s, d = lib
    bundle.sync_bundle(cfg, db, s)
    p = os.path.join(d, bundle.MANIFEST)
    txt = open(p).read().replace(f"schema = {bundle.SCHEMA}",
                                 f"schema = {bundle.SCHEMA + 1}")
    open(p, "w").write(txt)
    with pytest.raises(bundle.BundleError) as exc:
        bundle.import_bundle(cfg, db, d)
    assert exc.value.code == "future_schema"


def test_corrupt_manifest_reports_rather_than_raises_in_scan(lib):
    cfg, db, s, d = lib
    bundle.sync_bundle(cfg, db, s)
    open(os.path.join(d, bundle.MANIFEST), "w").write("schema = [oops\n")
    out = bundle.scan(cfg, db, cfg.royalroad.library_dir)
    assert out[0]["ok"] is False and out[0]["code"] == "bad_manifest"


def test_export_is_atomic(lib):
    """A half-written state.json would be a silent data loss at import time."""
    cfg, db, s, d = lib
    bundle.sync_bundle(cfg, db, s)
    assert not [f for f in os.listdir(d) if f.endswith(".tmp")]
    json.loads(open(os.path.join(d, bundle.STATE)).read())   # parses


def test_every_chapter_column_is_exported():
    """A new column must be considered for the bundle, not silently dropped."""
    import sqlite3
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = DB(os.path.join(td, "s.db"))
        cols = {r["name"] for r in db.con.execute("PRAGMA table_info(chapters)")}
        db.close()
    ignored = {"id", "series_id"}
    missing = cols - ignored - set(bundle._CHAPTER_COLS)
    assert not missing, f"chapters columns absent from the bundle export: {missing}"


def test_new_series_gets_a_uuid_on_insert(tmp_path):
    """Regression: the migration backfill runs at open time, so a series added
    afterwards had uuid=NULL until the next process started."""
    db = DB(str(tmp_path / "s.db"))
    db.upsert_series(_Fic(rr_id="321", slug="later"))
    row = db.get_series("321")
    assert row["uuid"]
    first = row["uuid"]
    db.upsert_series(_Fic(rr_id="321", slug="later"))     # re-add must not re-identify
    assert db.get_series("321")["uuid"] == first
    db.close()


# -- migration (plan step 3) ------------------------------------------------

def _legacy_layout(cfg, db, s, d):
    """Populate a bundle in the *pre-migration* shape: chapters flat in the
    series dir, config/lexicon in the shared data/ directories."""
    import csv
    names = []
    for i, c in enumerate(db.chapters(s["id"]), 1):
        for ext in (".md", ".opus", ".segments.json"):
            p = os.path.join(d, f"{i:03d}-{c['slug']}{ext}")
            if ext == ".segments.json":
                json.dump([{"kind": "speech", "text": f"line {i}", "voice": "am_michael",
                            "style": "narration", "rate": 1.0, "pitch": 0.0}],
                          open(p, "w"))
            else:
                open(p, "w").write("x")
            names.append(os.path.basename(p))
        db.con.execute("UPDATE chapters SET audio_path=?, text_path=? WHERE id=?",
                       (os.path.join(d, f"{i:03d}-{c['slug']}.opus"),
                        os.path.join(d, f"{i:03d}-{c['slug']}.md"), c["id"]))
    os.makedirs(os.path.join(d, ".raw"), exist_ok=True)
    open(os.path.join(d, ".raw", "999-0.html"), "w").write("<html>")
    db.con.execute("UPDATE chapters SET raw_path=? WHERE ord=0",
                   (os.path.join(d, ".raw", "999-0.html"),))
    open(os.path.join(d, "cover.jpg"), "wb").close()
    open(os.path.join(d, "cover-v10.jpg"), "wb").close()
    db.con.commit()

    legacy = bundle.legacy_paths(cfg, "test-series")
    for key, body in (("config", '[voices]\nnarrator = "af_nova"\n'),
                      ("lexicon", "surface,respell,ipa,notes\nKael,kale,,\n")):
        os.makedirs(os.path.dirname(legacy[key]), exist_ok=True)
        open(legacy[key], "w").write(body)
    return names


@pytest.fixture
def legacy(lib, tmp_path):
    cfg, db, s, d = lib
    cfg.general.series_config_dir = str(tmp_path / "data" / "series")
    cfg.general.lexicon_dir = str(tmp_path / "data" / "lexicons")
    cfg.general.cache_dir = str(tmp_path / "cache")
    _legacy_layout(cfg, db, s, d)
    return cfg, db, s, d


def test_migrate_moves_files_into_the_bundle_layout(legacy):
    cfg, db, s, d = legacy
    res = bundle.migrate(cfg, db, "test-series", log=lambda *_: None)[0]
    assert res["moved"] == 4 * 3 + 2 + 2          # chapters + covers + config/lexicon

    p = bundle.paths(d)
    assert os.path.isfile(os.path.join(p["chapters"], "001-chapter-1.opus"))
    assert os.path.isfile(os.path.join(p["covers"], "cover.jpg"))
    assert os.path.isfile(os.path.join(p["covers"], "cover-v10.jpg"))
    assert os.path.isfile(p["config"]) and os.path.isfile(p["lexicon"])
    assert 'narrator = "af_nova"' in open(p["config"]).read()
    # nothing left loose in the bundle root except the machine-owned files
    loose = {f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))}
    assert loose == {bundle.MANIFEST, bundle.STATE, "config.toml", "lexicon.csv"}
    # the old shared copies are gone, not duplicated
    assert not os.path.exists(bundle.legacy_paths(cfg, "test-series")["config"])


def test_migrate_rewrites_db_paths_to_the_new_location(legacy):
    cfg, db, s, d = legacy
    bundle.migrate(cfg, db, "test-series", log=lambda *_: None)
    for c in db.chapters(s["id"]):
        for col in ("audio_path", "text_path", "raw_path"):
            if c[col]:
                assert os.path.isfile(c[col]), f"{col} -> {c[col]}"
    c0 = db.chapters(s["id"])[0]
    assert c0["audio_path"] == os.path.join(d, "chapters", "001-chapter-1.opus")
    assert c0["raw_path"] == os.path.join(d, ".raw", "999-0.html")


def test_migrate_hardlinks_the_cache_without_copying(legacy):
    """Cache entries move by hard link: a key shared by two series can live in
    both bundles for the price of a directory entry."""
    import hashlib
    cfg, db, s, d = legacy
    src = os.path.join(cfg.general.cache_dir, "kokoro")
    os.makedirs(src, exist_ok=True)
    mat = f"line 1|am_michael|narration|1.0|0.0|{cfg.synth.sample_rate}"
    digest = hashlib.sha1(mat.encode()).hexdigest()
    open(os.path.join(src, f"{digest}.wav"), "wb").write(b"RIFFfake")
    open(os.path.join(src, "deadbeef" * 5 + ".wav"), "wb").write(b"orphan")

    res = bundle.migrate(cfg, db, "test-series", log=lambda *_: None)[0]
    assert res["cache_linked"] == 1
    linked = os.path.join(d, ".cache", "kokoro", f"{digest}.wav")
    assert os.path.isfile(linked)
    assert os.stat(linked).st_nlink == 2                  # same inode, not a copy
    assert os.path.isfile(os.path.join(src, f"{digest}.wav"))
    # an unreferenced entry is left for `cache prune`, not silently destroyed
    assert os.path.isfile(os.path.join(src, "deadbeef" * 5 + ".wav"))


def test_migrate_is_idempotent(legacy):
    cfg, db, s, d = legacy
    first = bundle.migrate(cfg, db, "test-series", log=lambda *_: None)[0]
    second = bundle.migrate(cfg, db, "test-series", log=lambda *_: None)[0]
    assert first["moved"] and second["moved"] == 0
    assert os.path.isfile(os.path.join(d, "chapters", "001-chapter-1.opus"))


def test_migrate_dry_run_writes_nothing(legacy):
    cfg, db, s, d = legacy
    before = sorted(os.listdir(d))
    res = bundle.migrate(cfg, db, "test-series", dry_run=True,
                         log=lambda *_: None)[0]
    assert res["moved"] == 16
    assert sorted(os.listdir(d)) == before
    assert not os.path.isdir(os.path.join(d, "chapters"))


def test_migrate_skips_a_missing_bundle(legacy):
    import shutil
    cfg, db, s, d = legacy
    shutil.rmtree(d)
    assert bundle.migrate(cfg, db, "test-series",
                          log=lambda *_: None)[0]["skipped"] == "missing"


def test_round_trip_still_exact_after_migration(legacy, tmp_path):
    """The step-2 guarantee must survive step 3."""
    cfg, db, s, d = legacy
    bundle.migrate(cfg, db, "test-series", log=lambda *_: None)
    before = _snapshot(db, s["id"])
    fresh = DB(str(tmp_path / "after.db"))
    bundle.import_bundle(cfg, fresh, d)
    new = fresh.get_series("999")
    after = _snapshot(fresh, new["id"])
    assert after["chapters"] == before["chapters"]
    assert after["series"] == before["series"]
    fresh.close()


def test_series_cfg_reads_the_bundle_after_migration(legacy):
    from webnovel_audio import sync
    cfg, db, s, d = legacy
    bundle.migrate(cfg, db, "test-series", log=lambda *_: None)
    scfg = sync._series_cfg(cfg, "test-series", d)
    assert scfg.voices.narrator == "af_nova"                 # bundle config.toml
    assert scfg.general.lexicon == os.path.join(d, "lexicon.csv")
    assert scfg.general.cache_dir == os.path.join(d, ".cache")


# -- archive / purge / reclaim (plan step 5) --------------------------------

def _members(path):
    import tarfile
    with tarfile.open(path) as tf:
        return sorted(m.name for m in tf.getmembers())


def test_archive_excludes_the_cache_by_default(legacy, tmp_path):
    cfg, db, s, d = legacy
    bundle.migrate(cfg, db, "test-series", log=lambda *_: None)
    os.makedirs(os.path.join(d, ".cache", "kokoro"), exist_ok=True)
    open(os.path.join(d, ".cache", "kokoro", "ab.wav"), "wb").write(b"x" * 4096)

    out = str(tmp_path / "t.tar")
    res = bundle.archive(cfg, db, "test-series", out=out, log=lambda *_: None)
    names = _members(out)
    assert res["ok"] and not res["with_cache"]
    assert not any("/.cache/" in n for n in names)
    assert "test-series/manifest.toml" in names
    assert "test-series/state.json" in names
    assert "test-series/config.toml" in names
    assert "test-series/lexicon.csv" in names
    assert "test-series/chapters/001-chapter-1.opus" in names
    assert "test-series/covers/cover.jpg" in names
    assert "test-series/.raw/999-0.html" in names


def test_archive_with_cache_includes_it(legacy, tmp_path):
    cfg, db, s, d = legacy
    bundle.migrate(cfg, db, "test-series", log=lambda *_: None)
    os.makedirs(os.path.join(d, ".cache", "kokoro"), exist_ok=True)
    open(os.path.join(d, ".cache", "kokoro", "ab.wav"), "wb").write(b"x" * 4096)
    out = str(tmp_path / "t2.tar")
    bundle.archive(cfg, db, "test-series", out=out, with_cache=True,
                   log=lambda *_: None)
    assert "test-series/.cache/kokoro/ab.wav" in _members(out)


def test_archive_refreshes_the_machine_owned_files_first(legacy, tmp_path):
    """An archive must carry current state, not whatever was last exported."""
    import tarfile
    cfg, db, s, d = legacy
    bundle.migrate(cfg, db, "test-series", log=lambda *_: None)
    db.con.execute("UPDATE chapters SET status='rendered' WHERE ord=3")
    db.con.commit()
    out = str(tmp_path / "t3.tar")
    bundle.archive(cfg, db, "test-series", out=out, log=lambda *_: None)
    with tarfile.open(out) as tf:
        st = json.load(tf.extractfile("test-series/state.json"))
    assert st["chapters"][3]["status"] == "rendered"


def test_archive_restores_into_a_fresh_db(legacy, tmp_path):
    """The round trip that makes an archive worth keeping: untar, import, and
    the series is tracked again with its state intact."""
    import tarfile
    cfg, db, s, d = legacy
    bundle.migrate(cfg, db, "test-series", log=lambda *_: None)
    before = _snapshot(db, s["id"])
    out = str(tmp_path / "t4.tar")
    bundle.archive(cfg, db, "test-series", out=out, log=lambda *_: None)

    dest = tmp_path / "restored"
    dest.mkdir()
    with tarfile.open(out) as tf:
        tf.extractall(dest, filter="data")
    fresh = DB(str(tmp_path / "restored.db"))
    res = bundle.import_bundle(cfg, fresh, str(dest / "test-series"))
    assert res["action"] == "add"
    new = fresh.get_series("999")

    # every column but the paths is identical; the paths are re-anchored to
    # wherever the archive was unpacked, which is the point
    keep = [i for i, c in enumerate(bundle._CHAPTER_COLS)
            if c not in bundle._PATH_COLS]
    strip = lambda rows: [tuple(r[i] for i in keep) for r in rows]   # noqa: E731
    assert strip(_snapshot(fresh, new["id"])["chapters"]) == strip(before["chapters"])
    c0 = fresh.chapters(new["id"])[0]
    assert c0["audio_path"] == str(dest / "test-series" / "chapters"
                                   / "001-chapter-1.opus")
    assert os.path.isfile(c0["audio_path"])
    fresh.close()


def test_archive_rejects_an_unknown_format(legacy, tmp_path):
    cfg, db, s, d = legacy
    with pytest.raises(bundle.BundleError) as exc:
        bundle.archive(cfg, db, "test-series", out=str(tmp_path / "x.7z"),
                       log=lambda *_: None)
    assert exc.value.code == "bad_archive_format"


def test_archive_reports_a_missing_bundle(legacy, tmp_path):
    import shutil
    cfg, db, s, d = legacy
    shutil.rmtree(d)
    with pytest.raises(bundle.BundleError) as exc:
        bundle.archive(cfg, db, "test-series", out=str(tmp_path / "x.tar"),
                       log=lambda *_: None)
    assert exc.value.code == "bundle_missing"


def test_purge_removes_everything_including_the_cache(legacy):
    cfg, db, s, d = legacy
    bundle.migrate(cfg, db, "test-series", log=lambda *_: None)
    os.makedirs(os.path.join(d, ".cache", "kokoro"), exist_ok=True)
    open(os.path.join(d, ".cache", "kokoro", "ab.wav"), "wb").write(b"x" * 8192)

    res = bundle.purge(cfg, db, db.get_series("999"))
    assert res["removed"] and res["bytes"] > 8000
    assert not os.path.exists(d)


def test_purge_of_a_missing_bundle_is_not_an_error(legacy):
    import shutil
    cfg, db, s, d = legacy
    shutil.rmtree(d)
    res = bundle.purge(cfg, db, db.get_series("999"))
    assert res["removed"] is False and res["bytes"] == 0


def test_du_counts_hardlinked_inodes_once(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    (a / "f").write_bytes(b"x" * 1000)
    os.link(a / "f", a / "g")
    size, files = bundle.du(str(a))
    assert files == 2 and size == 1000


def test_reclaim_unlinks_the_global_copy_so_deleting_frees_space(legacy):
    """`migrate` links rather than moves, so the bytes stay pinned by the
    shared cache until the duplicate link goes."""
    import hashlib
    cfg, db, s, d = legacy
    src = os.path.join(cfg.general.cache_dir, "kokoro")
    os.makedirs(src, exist_ok=True)
    mat = f"line 1|am_michael|narration|1.0|0.0|{cfg.synth.sample_rate}"
    digest = hashlib.sha1(mat.encode()).hexdigest()
    open(os.path.join(src, f"{digest}.wav"), "wb").write(b"x" * 2048)
    open(os.path.join(src, "cafe" * 10 + ".wav"), "wb").write(b"orphan")

    bundle.migrate(cfg, db, "test-series", log=lambda *_: None)
    linked = os.path.join(d, ".cache", "kokoro", f"{digest}.wav")
    assert os.stat(linked).st_nlink == 2

    r = bundle.reclaim_cache(cfg, db)
    assert r["unlinked"] == 1 and r["bytes"] == 2048
    assert os.stat(linked).st_nlink == 1              # bundle holds the only ref
    assert not os.path.exists(os.path.join(src, f"{digest}.wav"))
    # an unclaimed entry is left for `cache prune`, never destroyed here
    assert os.path.isfile(os.path.join(src, "cafe" * 10 + ".wav"))
    assert r["kept"] == 1


def test_reclaim_dry_run_writes_nothing(legacy):
    import hashlib
    cfg, db, s, d = legacy
    src = os.path.join(cfg.general.cache_dir, "kokoro")
    os.makedirs(src, exist_ok=True)
    mat = f"line 1|am_michael|narration|1.0|0.0|{cfg.synth.sample_rate}"
    digest = hashlib.sha1(mat.encode()).hexdigest()
    open(os.path.join(src, f"{digest}.wav"), "wb").write(b"x" * 2048)
    bundle.migrate(cfg, db, "test-series", log=lambda *_: None)
    r = bundle.reclaim_cache(cfg, db, dry_run=True)
    assert r["unlinked"] == 1
    assert os.path.isfile(os.path.join(src, f"{digest}.wav"))


def test_reclaim_keeps_a_key_not_yet_in_every_owning_bundle(legacy, tmp_path):
    """Two series sharing a segment: the global link may only go once both
    bundles hold their own."""
    import hashlib
    cfg, db, s, d = legacy
    other = _Fic(rr_id="777", slug="other-series")
    sid2 = db.upsert_series(other)
    db.replace_chapters(sid2, other.chapters)
    d2 = os.path.join(cfg.royalroad.library_dir, "other-series")
    os.makedirs(os.path.join(d2, "chapters"), exist_ok=True)
    json.dump([{"kind": "speech", "text": "line 1", "voice": "am_michael",
                "style": "narration", "rate": 1.0, "pitch": 0.0}],
              open(os.path.join(d2, "chapters", "001-x.segments.json"), "w"))

    src = os.path.join(cfg.general.cache_dir, "kokoro")
    os.makedirs(src, exist_ok=True)
    mat = f"line 1|am_michael|narration|1.0|0.0|{cfg.synth.sample_rate}"
    digest = hashlib.sha1(mat.encode()).hexdigest()
    open(os.path.join(src, f"{digest}.wav"), "wb").write(b"x" * 2048)

    bundle.migrate(cfg, db, "test-series", log=lambda *_: None)   # only one
    r = bundle.reclaim_cache(cfg, db)
    assert r["unlinked"] == 0 and r["kept"] == 1
    assert os.path.isfile(os.path.join(src, f"{digest}.wav"))
