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
