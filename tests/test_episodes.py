"""Multi-chapter feed episodes: grouping, publishing rules, the joined file, the feed."""
import datetime as dt
import json
import os
import shutil
import subprocess
from xml.etree import ElementTree as ET

import pytest

from webnovel_audio import episodes
from webnovel_audio.config import Config
from webnovel_audio.feed import build_feed

NOW = dt.datetime(2026, 11, 24, tzinfo=dt.timezone.utc)


def _ch(tmp_path, i, *, status="rendered", vol="v1", pub="2021-10-01T00:00:00Z",
        audio=True, seconds=None):
    p = tmp_path / f"{i + 1:03d}-c.opus"
    if audio and status == "rendered" and not p.exists():
        if seconds:
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            f"sine=frequency={300 + 40 * i}:duration={seconds}",
                            "-ac", "1", "-c:a", "libopus", "-b:a", "24k", str(p)],
                           check=True)
        else:
            p.write_bytes(b"OggS" + b"\0" * 100)
    return {"ord": i, "source_id": str(5000 + i), "title": f"{i + 1}. Title {i + 1}",
            "status": status, "audio_path": str(p) if status == "rendered" else None,
            "duration_s": float(seconds or 60), "published_at": pub,
            "volume_source_id": vol, "url": f"https://rr/c/{i}"}


def test_groups_of_n_restart_at_each_volume(tmp_path):
    chs = [_ch(tmp_path, i, vol="v1" if i < 7 else "v2") for i in range(12)]
    eps = episodes.plan(chs, 5, now=NOW)
    assert [[c["ord"] + 1 for c in e.members] for e in eps] == \
        [[1, 2, 3, 4, 5], [6, 7], [8, 9, 10, 11, 12]]
    assert [e.index for e in eps] == [1, 2, 3]
    assert eps[1].full is False                 # volume ended: published short
    assert eps[0].name == "e0001-0005.opus"
    assert eps[0].title == "1. Title 1 – 5. Title 5"


def test_only_a_leading_run_of_rendered_groups_is_published(tmp_path):
    chs = [_ch(tmp_path, i) for i in range(15)]
    chs[7] = _ch(tmp_path, 7, status="new")     # a hole in the second group
    eps = episodes.plan(chs, 5, now=NOW)
    # the third group is complete, but publishing it before the second would
    # give the second an older date than an episode already in the feed
    assert [e.index for e in eps] == [1]


def test_skipped_chapters_are_left_out_not_blocking(tmp_path):
    chs = [_ch(tmp_path, i) for i in range(6)]
    chs[2] = _ch(tmp_path, 2, status="skipped")
    eps = episodes.plan(chs, 5, now=NOW, partial_after_days=7)
    assert [c["ord"] + 1 for c in eps[0].members] == [1, 2, 4, 5, 6]


def test_the_short_tail_waits_until_its_newest_chapter_is_old(tmp_path):
    fresh = (NOW - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    chs = [_ch(tmp_path, i) for i in range(5)] + [_ch(tmp_path, 5, pub=fresh)]
    assert len(episodes.plan(chs, 5, now=NOW, partial_after_days=7)) == 1
    assert len(episodes.plan(chs, 5, now=NOW, partial_after_days=1)) == 2


def test_bad_chapters_per_episode_is_an_error():
    cfg = Config()
    for bad in (0, -2, "5", 2.5, True):
        cfg.feed.chapters_per_episode = bad
        with pytest.raises(SystemExit):
            episodes.per_episode(cfg)
    cfg.feed.chapters_per_episode = 5
    assert episodes.per_episode(cfg) == 5


def test_series_config_sets_it(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[feed]\nchapters_per_episode = 5\n")
    sc = Config().overlay(str(p))
    assert sc.feed.chapters_per_episode == 5 and sc.feed.partial_after_days == 7


def _series():
    return {"slug": "demo", "title": "Demo", "author": "Jo", "provider": "royalroad",
            "url": "https://rr/9", "cover_url": ""}


def test_feed_lists_episodes_with_stable_ids(tmp_path):
    chs = [_ch(tmp_path, i, pub=f"2021-10-{i + 1:02d}T00:00:00Z") for i in range(8)]
    eps = episodes.plan(chs, 5, now=NOW)
    for e in eps:
        e.path = e.members[0]["audio_path"]     # any existing file will do here
    root = ET.fromstring(build_feed(_series(), chs, "http://h", episodes=eps))
    items = root.findall("./channel/item")
    assert [i.findtext("guid") for i in items] == ["rr-5005-x3", "rr-5000-x5"]
    assert items[1].findtext("title") == "1. Title 1 – 5. Title 5"
    assert "/audio/demo/episodes/" in items[0].find("enclosure").get("url")
    ns = {"it": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
    assert [i.findtext("it:episode", namespaces=ns) for i in items] == ["2", "1"]
    assert "Title 3" in items[1].findtext("description")   # chapters listed


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_joined_file_has_a_chapter_mark_per_chapter(tmp_path):
    chs = [_ch(tmp_path, i, seconds=1) for i in range(3)]
    ep = episodes.plan(chs, 3, now=NOW)[0]
    out = tmp_path / "episodes" / ep.name
    episodes._write(ep, str(out), {"title": "Demo", "author": "Jo"}, "Book One")
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-show_streams", "-show_format",
         "-of", "json", str(out)], capture_output=True, text=True, check=True).stdout)
    assert [c["tags"]["title"] for c in probe["chapters"]] == \
        ["1. Title 1", "2. Title 2", "3. Title 3"]
    tags = {k.lower(): v for k, v in probe["streams"][0]["tags"].items()}
    assert tags["title"] == "1. Title 1 – 3. Title 3"
    assert tags["album"] == "Demo — Book One" and tags["genre"] == "Audiobook"
    assert abs(float(probe["format"]["duration"]) - 3.0) < 0.2
    assert episodes._fresh(str(out), ep)
    os.utime(chs[1]["audio_path"], ns=(1, 1))   # a re-rendered chapter
    assert not episodes._fresh(str(out), ep)
