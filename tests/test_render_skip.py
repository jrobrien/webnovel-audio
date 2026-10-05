import os
import shutil

import pytest

from webnovel_audio import pipeline
from webnovel_audio.config import Config

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
FP = "null-fp-1"


@pytest.fixture
def chapter(tmp_path):
    src = tmp_path / "ch.txt"
    src.write_text("The first paragraph.\n\nAnother one, a little longer than the first.\n")
    cfg = Config()
    cfg.general.cache_dir = str(tmp_path / "cache")
    cfg.audio.master_jobs = 1

    def render(**kw):
        kw.setdefault("skip_unchanged", True)
        kw.setdefault("fingerprint", FP)
        return pipeline.render(str(src), str(tmp_path / "ch.opus"), cfg, backend="null",
                               log=lambda *_: None, **kw)

    render.src, render.cfg, render.out = src, cfg, tmp_path / "ch.opus"
    return render


def test_an_unchanged_chapter_is_left_exactly_as_it_was(chapter):
    first = chapter()
    assert not first.skipped and chapter.out.exists()
    assert (chapter.out.parent / "ch.recipe").exists()
    before = chapter.out.stat().st_mtime_ns

    again = chapter()
    assert again.skipped and again.out_path == str(chapter.out)
    assert chapter.out.stat().st_mtime_ns == before                  # not rewritten
    assert again.cached_segments == again.total_segments


def test_anything_that_changes_the_audio_re_renders_it(chapter):
    chapter()
    chapter.src.write_text("The first paragraph.\n\nAnother one, now said differently.\n")
    assert not chapter().skipped                                       # the text changed
    assert chapter().skipped
    chapter.cfg.audio.opus_bitrate = "40k"
    assert not chapter().skipped                                       # how it is encoded changed
    assert chapter().skipped
    chapter.cfg.audio.mastering = "loudnorm"
    assert not chapter().skipped                                       # how it is mastered changed
    assert not chapter(fingerprint="null-fp-2").skipped                # a new synth generation
    chapter.cfg.dsp = {**chapter.cfg.dsp, "narration": {"gain_db": -2.0}}
    assert not chapter(fingerprint="null-fp-2").skipped                # an effect chain changed


def test_scheduling_settings_do_not_count_as_a_change(chapter):
    chapter()
    chapter.cfg.audio.master_jobs = 6
    assert chapter().skipped


def test_a_chapter_with_no_recipe_or_no_file_is_rendered_not_skipped(chapter):
    chapter()
    (chapter.out.parent / "ch.recipe").unlink()                        # rendered before recipes existed
    assert not chapter().skipped
    assert chapter().skipped                                           # skippable from then on
    chapter.out.unlink()
    assert not chapter().skipped                                       # the audio itself is gone
    assert chapter.out.exists()


def test_force_and_one_off_renders_never_skip(chapter):
    chapter()
    assert not chapter(skip_unchanged=False).skipped                   # --force
    assert not chapter(fingerprint="").skipped                         # a one-off file has no fingerprint


def test_a_dry_run_writes_no_audio(chapter, tmp_path):
    rep = chapter(dry_run=True)
    assert not rep.skipped and not chapter.out.exists()


def test_prepare_then_master_is_what_render_does(chapter, tmp_path):
    job = pipeline.prepare(str(chapter.src), str(tmp_path / "split.opus"), chapter.cfg,
                           backend="null", log=lambda *_: None, fingerprint=FP)
    assert job.wav is not None and not (tmp_path / "split.opus").exists()   # nothing mastered yet
    rep = pipeline.master(job)
    assert (tmp_path / "split.opus").exists() and job.wav is None           # freed after mastering
    assert rep.size_bytes == (tmp_path / "split.opus").stat().st_size and rep.audio_seconds > 0
