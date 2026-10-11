"""`series rename`: the slug changes everywhere it is recorded, the old one keeps
resolving (a podcast app is subscribed to /feed/<slug>.xml), and nothing is lost."""
import http.client
import json
import os
import threading
import tomllib

import pytest

from webnovel_audio import bundle, sync
from webnovel_audio.config import Config
from webnovel_audio.db import DB
from webnovel_audio.providers import ChapterRef, SeriesInfo
from webnovel_audio.safepath import series_slug, valid_slug


def _fic(source_id="9", title="Between Beast And Buddha: A Drunken Monkey's Journey"):
    return SeriesInfo(provider="royalroad", source_id=source_id, slug="site-slug",
                      title=title, url=f"https://rr/{source_id}")


@pytest.fixture
def lib(tmp_path):
    cfg = Config()
    cfg.library.state_db = str(tmp_path / "state.db")
    cfg.library.library_dir = str(tmp_path / "library")
    cfg.general.base_lexicon = str(tmp_path / "_base.csv")
    (tmp_path / "_base.csv").write_text("surface,respell,ipa,notes\n")
    db = DB(cfg.library.state_db)
    sid = db.upsert_series(_fic())
    db.replace_chapters(sid, [ChapterRef(source_id="c1", order=0, title="One", slug="one",
                                         url="https://rr/c1", unlocked=True)])
    slug = db.series_by_id(sid)["slug"]
    d = os.path.join(cfg.library.library_dir, slug)
    os.makedirs(os.path.join(d, "chapters"))
    audio = os.path.join(d, "chapters", "001-one.opus")
    open(audio, "wb").write(b"opus")
    db.mark(db.chapters(sid)[0]["id"], "rendered", audio_path=audio)
    bundle.sync_bundle(cfg, db, db.series_by_id(sid))
    yield cfg, db, sid, d
    db.close()


def test_auto_slug_is_short_and_cut_between_words():
    s = series_slug("Between Beast And Buddha: A Drunken Monkey's Journey to Immortality")
    assert s == "between-beast-and-buddha-a-drunken"
    assert "monkey-s" not in series_slug("The Drunken Monkey's Way")
    assert series_slug("The Drunken Monkey's Way") == "the-drunken-monkeys-way"
    assert series_slug("x" * 80) == "x" * 40            # one long word: hard cut


def test_valid_slug():
    assert valid_slug("drunken-monkey") is None
    for bad in ("", "Drunken", "a b", "-a", "a-", "a--b", "../x", "x" * 61):
        assert valid_slug(bad), bad


def test_rename_changes_db_manifest_and_state_and_keeps_the_old_name(lib):
    cfg, db, sid, d = lib
    old = db.series_by_id(sid)["slug"]
    info = sync.rename_series(cfg, db, old, "drunken-monkey")
    assert info["slug"] == "drunken-monkey" and info["old_slug"] == old
    assert db.series_by_id(sid)["slug"] == "drunken-monkey"
    # both names find it; the new one is canonical
    assert db.get_series(old)["id"] == sid
    assert db.get_series("drunken-monkey")["id"] == sid
    with open(os.path.join(d, "manifest.toml"), "rb") as fh:
        assert tomllib.load(fh)["slug"] == "drunken-monkey"
    st = json.load(open(os.path.join(d, "state.json")))
    assert st["series"]["slug"] == "drunken-monkey"
    # the directory is untouched unless asked
    assert os.path.isdir(d) and bundle.bundle_dir(cfg, db.series_by_id(sid)) == os.path.abspath(d)


def test_rename_with_move_relocates_the_bundle_and_repoints_the_chapters(lib):
    cfg, db, sid, d = lib
    sync.rename_series(cfg, db, "drunken", "drunken-monkey", move=True)
    new = os.path.join(cfg.library.library_dir, "drunken-monkey")
    assert os.path.isdir(new) and not os.path.exists(d)
    row = db.series_by_id(sid)
    assert row["path"] == new
    c = db.chapters(sid)[0]
    assert c["audio_path"] == os.path.join(new, "chapters", "001-one.opus")
    assert os.path.exists(c["audio_path"])


def test_dry_run_changes_nothing(lib):
    cfg, db, sid, d = lib
    old = db.series_by_id(sid)["slug"]
    info = sync.rename_series(cfg, db, old, "drunken-monkey", move=True, dry_run=True)
    assert info["dry_run"] and info["moved"] and info["slug"] == "drunken-monkey"
    assert db.series_by_id(sid)["slug"] == old and os.path.isdir(d)


def test_rename_refuses_a_taken_or_malformed_slug(lib):
    cfg, db, sid, d = lib
    other = db.upsert_series(_fic("10", "Sky Pride"))
    with pytest.raises(sync.SlugError) as e:
        sync.rename_series(cfg, db, "drunken", "sky-pride")
    assert e.value.code == "slug_taken"
    with pytest.raises(sync.SlugError) as e:
        sync.rename_series(cfg, db, "drunken", "Not Valid")
    assert e.value.code == "bad_slug"
    # nor an unrelated directory already in the library
    os.makedirs(os.path.join(cfg.library.library_dir, "stray"))
    with pytest.raises(sync.SlugError):
        sync.rename_series(cfg, db, "drunken", "stray")
    # nor the old name of another series
    sync.rename_series(cfg, db, "sky-pride", "sky")
    with pytest.raises(sync.SlugError):
        sync.rename_series(cfg, db, "drunken", "sky-pride")
    assert db.series_by_id(other)["slug"] == "sky"


def test_rename_back_to_an_old_name_works(lib):
    cfg, db, sid, d = lib
    old = db.series_by_id(sid)["slug"]
    sync.rename_series(cfg, db, old, "drunken-monkey")
    sync.rename_series(cfg, db, "drunken-monkey", old)
    assert db.series_by_id(sid)["slug"] == old
    assert db.alias_series(old) is None                 # not its own alias


def test_rename_waits_for_a_running_sync(lib):
    cfg, db, sid, d = lib
    with sync.sync_lock(cfg):
        with pytest.raises(sync.SyncLocked):
            sync.rename_series(cfg, db, "drunken", "drunken-monkey")
    assert db.series_by_id(sid)["slug"] != "drunken-monkey"


def test_unknown_series_is_reported(lib):
    cfg, db, *_ = lib
    with pytest.raises(sync.SlugError) as e:
        sync.rename_series(cfg, db, "nope", "x")
    assert e.value.code == "no_such_series"


def test_old_feed_url_redirects_to_the_new_one(lib):
    from webnovel_audio.serve import _Handler, _Server
    cfg, db, sid, d = lib
    old = db.series_by_id(sid)["slug"]
    sync.rename_series(cfg, db, old, "drunken-monkey")
    httpd = _Server(("127.0.0.1", 0), _Handler)
    httpd.cfg, httpd.access = cfg, None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]

        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", path)
            r = c.getresponse()
            r.read()
            return r.status, r.getheader("Location")

        assert get(f"/feed/{old}.xml") == (301, "/feed/drunken-monkey.xml")
        assert get("/feed/drunken-monkey.xml")[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_add_with_a_slug_overrides_the_title_and_only_for_new_series(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    sid = db.upsert_series(_fic(), slug="drunken-monkey")
    assert db.series_by_id(sid)["slug"] == "drunken-monkey"
    assert db.upsert_series(_fic(), slug="other") == sid            # refresh keeps it
    assert db.series_by_id(sid)["slug"] == "drunken-monkey"
    db.close()


def test_old_database_without_the_alias_table_still_resolves(tmp_path):
    """The server opens the DB without creating tables; a lookup must not crash."""
    db = DB(str(tmp_path / "s.db"))
    db.upsert_series(_fic(), slug="a")
    db.con.execute("DROP TABLE series_aliases")
    assert db.get_series("a")["slug"] == "a"
    assert db.get_series("zzz") is None
    db.close()


def _cli(tmp_path, *argv):
    """Run the real CLI against the fixture's library; returns (rc, parsed stdout)."""
    import contextlib
    import io

    from webnovel_audio import cli
    conf = tmp_path / "c.toml"
    conf.write_text(f'[library]\nstate_db = "{tmp_path / "state.db"}"\n'
                    f'library_dir = "{tmp_path / "library"}"\n'
                    f'[general]\nbase_lexicon = "{tmp_path / "_base.csv"}"\n')
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main([*argv, "-c", str(conf), "--json"])
    return rc, json.loads(buf.getvalue())


def test_cli_rename_reports_json_and_refuses_cleanly(lib, tmp_path):
    cfg, db, sid, d = lib
    rc, out = _cli(tmp_path, "series", "rename", "--scope", "drunken", "--slug", "drunken-monkey",
                   "--move", "--dry-run")
    assert rc == 0 and out["dry_run"] and out["slug"] == "drunken-monkey" and out["moved"]
    rc, out = _cli(tmp_path, "series", "rename", "--scope", "drunken", "--slug", "Bad Slug")
    assert rc == 1 and out["error"]["code"] == "bad_slug"
    rc, out = _cli(tmp_path, "series", "rename", "--scope", "nope", "--slug", "x")
    assert rc == 1 and out["error"]["code"] == "no_such_series"
    rc, out = _cli(tmp_path, "series", "rename", "--scope", "drunken", "--slug", "drunken-monkey")
    assert rc == 0 and out["old_slug"].startswith("between-beast")
    rc, out = _cli(tmp_path, "series", "show", "--scope", "drunken-monkey")
    assert rc == 0


def test_cli_add_dry_run_previews_without_tracking(lib, tmp_path, monkeypatch):
    cfg, db, sid, d = lib
    seen = {}

    def fake(cfg_, url):
        seen["url"] = url
        return {"title": "T", "author": "A", "provider": "royalroad", "chapters": 3,
                "slug": "t", "tracked": False, "slug_free": True}

    monkeypatch.setattr(sync, "preview_series", fake)
    monkeypatch.setattr(sync, "add_series",
                        lambda *a, **k: pytest.fail("a dry run must not track anything"))
    rc, out = _cli(tmp_path, "series", "add", "--url", "https://rr/fiction/1", "--dry-run")
    assert rc == 0 and out["dry_run"] and out["slug"] == "t" and seen["url"].endswith("/1")


def test_cli_add_with_a_taken_slug_fails_with_a_code(lib, tmp_path, monkeypatch):
    cfg, db, sid, d = lib

    def boom(*a, **k):
        raise sync.SlugError("slug_taken", "'x' already belongs to 'Y'")

    monkeypatch.setattr(sync, "add_series", boom)
    rc, out = _cli(tmp_path, "series", "add", "--url", "https://rr/fiction/1", "--slug", "x")
    assert rc == 1 and out["error"]["code"] == "slug_taken"
