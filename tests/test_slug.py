from webnovel_audio.db import DB
from webnovel_audio.providers import SeriesInfo
from webnovel_audio.safepath import series_slug


def test_series_slug_drops_bracketed_promo_text():
    assert series_slug("Reforged from Ruin [Eldritch Xianxia Cultivation]") == "reforged-from-ruin"
    assert series_slug("Sky Pride [Vol. 1 Stubbing October 17. Read Now!]") == "sky-pride"
    assert series_slug(
        "I Made A Railgun with Lightning Magic (LitRPG / OP MC / Isekai) [Book 1 Completed]"
    ) == "i-made-a-railgun-with-lightning-magic"
    assert series_slug("Aura Overload") == "aura-overload"


def test_series_slug_falls_back_when_the_title_is_only_decoration():
    assert series_slug("[Completed]", "fiction-12") == "fiction-12"
    assert series_slug("", "fiction-12") == "fiction-12"


def test_series_slug_is_capped_on_a_clean_boundary():
    s = series_slug("word " * 30)
    assert len(s) <= 60 and not s.endswith("-")


def test_slug_is_set_once_and_survives_a_retitle(tmp_path):
    """The site's slug follows every retitle; the handle must not."""
    db = DB(str(tmp_path / "s.db"))
    info = SeriesInfo(provider="royalroad", source_id="7", slug="x-long-site-slug",
                      title="Sky Pride [Read Now!]", url="https://rr/7")
    sid = db.upsert_series(info)
    assert db.series_by_id(sid)["slug"] == "sky-pride"

    info.title, info.slug = "Sky Pride [Vol. 2 Stubbing Sunday]", "another-site-slug"
    assert db.upsert_series(info) == sid
    row = db.series_by_id(sid)
    assert row["slug"] == "sky-pride" and "Vol. 2" in row["title"]
    db.close()
