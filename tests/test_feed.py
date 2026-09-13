from xml.etree import ElementTree as ET

from webnovel_audio.feed import audio_mime, build_feed, hms, rfc822
from webnovel_audio.package import _ffmeta


def test_rr_date_formats_parse():
    # regression: the fractional-seconds + explicit-offset form must not double the tz
    assert rfc822("2026-09-07T03:06:02Z").startswith("Mon, 07 Sep 2026 03:06:02")
    assert rfc822("2026-09-08T03:56:53.0000000+00:00").startswith("Tue, 08 Sep 2026 03:56:53")
    assert rfc822("").endswith("+0000")  # falls back to now, still valid RFC822


def test_hms_and_mime():
    assert hms(1163) == "19:23"
    assert hms(3725) == "1:02:05"
    assert hms(0) == "" and hms(None) == ""
    assert audio_mime("x/y/018.opus") == "audio/ogg"
    assert audio_mime("a.m4b") == "audio/mp4"


def _series():
    return {"slug": "demo", "title": "Demo Series", "author": "Jo",
            "url": "https://rr/9", "cover_url": "https://cdn/x.jpg"}


def _chapter(tmp_path, ord_, status="rendered", dur=1163.0):
    p = tmp_path / f"{ord_ + 1:03d}-c.opus"
    p.write_bytes(b"OggS" + b"\0" * 900)
    return {"ord": ord_, "rr_id": str(1000 + ord_), "title": f"Chapter {ord_ + 1}",
            "status": status, "audio_path": str(p) if status == "rendered" else None,
            "duration_s": dur, "published_at": f"2026-09-0{ord_ + 1}T04:00:00Z",
            "rendered_at": "2026-09-09T00:00:00"}


def test_build_feed_structure(tmp_path):
    chapters = [_chapter(tmp_path, 0), _chapter(tmp_path, 1),
                _chapter(tmp_path, 2, status="new")]
    xml = build_feed(_series(), chapters, "http://host:8080",
                     self_url="http://host:8080/feed/demo.xml")
    root = ET.fromstring(xml)
    ns = {"it": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
    items = root.findall("./channel/item")
    assert len(items) == 2                              # the "new" chapter is excluded
    # newest first
    assert items[0].findtext("title") == "Chapter 2"
    enc = items[0].find("enclosure")
    assert enc.get("url").endswith("/audio/demo/002-c.opus")
    assert int(enc.get("length")) == 904
    assert enc.get("type") == "audio/ogg"
    assert items[0].findtext("it:episode", namespaces=ns) == "2"
    assert items[0].findtext("it:duration", namespaces=ns) == "19:23"
    assert items[1].findtext("guid") == "rr-1000"


def test_ffmeta_chapters_are_cumulative():
    marks = [(0, 1000, "One"), (1000, 3500, "Two; a=b")]
    meta = _ffmeta("Book", "Auth", marks)
    assert meta.startswith(";FFMETADATA1")
    assert "START=1000\nEND=3500" in meta
    assert "title=Two, a-b" in meta                     # ';' and '=' sanitised


def test_show_notes_carry_provenance():
    """The feed used to say only 'Series — chapter 69'. It should carry what the
    .opus already embeds: source, dates, voice."""
    from webnovel_audio.feed import _show_notes

    series = {"title": "Sky Pride", "author": "Warby Picus"}
    chapter = {
        "title": "Chapter 15- The Calculations of Heavenly People",
        "url": "https://www.royalroad.com/fiction/107917/x/chapter/2266790/y",
        "published_at": "2025-05-09T14:00:09Z",
        "rendered_at": "2026-09-12T18:17:11",
        "volume_chapter": 16,
        "narrator": "af_nova",
    }
    vol = {"title": "V. 2 Lotuses Above, Snakes Below", "index": 2}
    plain, html = _show_notes(series, chapter, vol, 69, "13:22")

    assert "V. 2 Lotuses Above, Snakes Below" in plain
    assert "by Warby Picus" in plain
    assert "published 2025-05-09" in plain and "narrated 2026-09-12" in plain
    assert "af_nova" in plain and "13:22" in plain
    assert chapter["url"] in plain
    assert "Not an official audiobook" in plain
    # the positional number must NOT be asserted as a chapter number: it drifts
    # from the author's numbering whenever a volume contains an interstitial
    assert "chapter 16" not in plain
    # html variant has a real clickable link, correctly quoted
    assert f'<a href="{chapter["url"]}">' in html
    assert "<p>" in html and "]]>" not in html


def test_show_notes_without_volume_or_voice():
    from webnovel_audio.feed import _show_notes

    plain, html = _show_notes({"title": "Demo", "author": ""},
                              {"title": "Ch 1", "url": "", "published_at": "",
                               "rendered_at": "", "volume_chapter": None,
                               "narrator": ""}, None, 1, "")
    assert "Demo" in plain and "Source:" not in plain
    assert "Narrated by webnovel-audio" in plain
    assert html.startswith("<p>")
