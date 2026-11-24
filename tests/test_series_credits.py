"""Series author / support links: stored, kept when a page omits them, corrected."""
import json

from webnovel_audio.db import DB
from webnovel_audio.providers.base import SeriesInfo


def _fi(**kw):
    return SeriesInfo(source_id="9", slug="d", title="Demo", provider="royalroad",
                      url="https://rr/9", **kw)


def test_a_page_without_support_links_keeps_the_stored_ones(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    sid = db.upsert_series(_fi(author="A", author_url="https://rr/p/1",
                               support_urls=["https://patreon.com/a"]))
    db.upsert_series(_fi(author="A"))                 # an older layout: no links
    row = db.series_by_id(sid)
    assert row["author_url"] == "https://rr/p/1"
    assert json.loads(row["support_urls"]) == ["https://patreon.com/a"]


def test_chapter_page_corrects_a_wrong_author(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    sid = db.upsert_series(_fi(author="My Profile"))
    assert db.set_series_credits(sid, "Real", "https://rr/p/2", ["https://ko-fi.com/r"])
    row = db.series_by_id(sid)
    assert (row["author"], row["author_url"]) == ("Real", "https://rr/p/2")
    assert not db.set_series_credits(sid, "Real", "", None)   # nothing new: no write
