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
