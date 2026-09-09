import os

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


def test_resolve_start_variants():
    chs = _chapters(10)
    assert sync._resolve_start(chs, "latest") == 9
    assert sync._resolve_start(chs, "start") == -1
    assert sync._resolve_start(chs, "4") == 3
    assert sync._resolve_start(chs, "https://rr/chapter/103/ch-4") == 3


def test_series_cfg_prefers_per_series_lexicon(tmp_path):
    cfg = Config()
    cfg.general.lexicon_dir = str(tmp_path)
    assert sync._series_cfg(cfg, "salvage-run") is cfg          # no file -> unchanged
    (tmp_path / "salvage-run.csv").write_text("surface,respell,ipa,notes\n")
    sc = sync._series_cfg(cfg, "salvage-run")
    assert sc is not cfg and sc.general.lexicon.endswith("salvage-run.csv")
    assert cfg.general.lexicon == ""                          # original untouched


class _FakeClient:
    def __init__(self, fiction_html):
        self.fiction_html = fiction_html
        self.hits = []

    def html(self, url):
        self.hits.append(url)
        return self.fiction_html if "/chapter/" not in url else CHAPTER_STUB

    def close(self):
        pass


def test_add_and_sync_offline(tmp_path, monkeypatch):
    if not os.path.exists(FICTION_FIXTURE):
        return
    fiction_html = open(FICTION_FIXTURE, encoding="utf-8").read()
    monkeypatch.setattr(sync, "_client", lambda cfg: _FakeClient(fiction_html))

    cfg = Config()
    cfg.royalroad.state_db = str(tmp_path / "state.db")
    cfg.royalroad.library_dir = str(tmp_path / "lib")

    sync.add_series(cfg, "https://www.royalroad.com/fiction/424242/salvage-run", start="4")
    db = DB(cfg.royalroad.state_db)
    s = db.get_series("424242")
    assert s["progress_order"] == 3
    assert len(db.pending(s["id"])) == 2                    # chapters 5, 6
    db.close()

    res = sync.run_sync(cfg, limit=1, backend="null", log=lambda *_: None)
    assert res.rendered == 1 and res.errors == 0

    db = DB(cfg.royalroad.state_db)
    s = db.get_series("424242")
    assert s["progress_order"] == 4                         # advanced by one
    done = [c for c in db.chapters(s["id"]) if c["status"] == "rendered"]
    assert len(done) == 1 and os.path.exists(done[0]["audio_path"])
    db.close()

    raw = tmp_path / "lib" / s["slug"] / ".raw"
    assert raw.is_dir() and any(raw.iterdir())              # chapter html cached
