import os
import re
import shutil
import subprocess

import numpy as np
import pytest

from webnovel_audio import audio

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
SR = 24000


def _speechlike(seconds=20, level=0.03, seed=1):
    """Phrase-shaped noise bursts: quiet enough to need a lot of gain."""
    rng = np.random.default_rng(seed)
    n = seconds * SR
    t = np.arange(n) / SR
    env = ((np.sin(2 * np.pi * 0.4 * t) ** 2) > 0.3) * (0.4 + 0.6 * np.abs(np.sin(2 * np.pi * 3 * t)))
    return (level * rng.standard_normal(n) * env).astype("float32")


def _max_db(path):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", "volumedetect",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    return float(re.search(r"max_volume:\s+(-?[\d.]+) dB", out).group(1))


def test_fast_mastering_lands_on_the_target_loudness_like_loudnorm(tmp_path):
    x = _speechlike()
    fast, slow = str(tmp_path / "fast.opus"), str(tmp_path / "slow.opus")
    audio.write_opus(x, SR, fast, mastering="fast")
    audio.write_opus(x, SR, slow, mastering="loudnorm")
    i_fast, i_slow = audio.measure_integrated(fast), audio.measure_integrated(slow)
    assert abs(i_fast - -19.0) < 0.8
    assert abs(i_fast - i_slow) < 0.8              # the two methods agree to well under 1 LU


def test_fast_mastering_limits_peaks_it_would_otherwise_clip(tmp_path):
    x = _speechlike(level=0.02)
    x[::SR // 2] = 0.95                              # sharp spikes that +20 dB of gain would blow through
    out = str(tmp_path / "spiky.opus")
    audio.write_opus(x, SR, out, mastering="fast")
    assert _max_db(out) < -1.0                       # limiter ceiling (-4 dBFS) plus codec overshoot


def test_fast_mastering_falls_back_when_the_audio_is_silent(tmp_path):
    out = str(tmp_path / "quiet.opus")
    audio.write_opus(np.zeros(SR * 3, dtype="float32"), SR, out, mastering="fast")
    assert os.path.getsize(out) > 0


def test_the_file_appears_whole_and_a_failed_encode_leaves_the_old_one(tmp_path):
    out = str(tmp_path / "ch.opus")
    audio.write_opus(_speechlike(5), SR, out, mastering="fast")
    assert [f for f in os.listdir(tmp_path)] == ["ch.opus"]          # no .part file left behind
    before = open(out, "rb").read()
    with pytest.raises(subprocess.CalledProcessError):
        audio.write_opus(_speechlike(5), SR, out, bitrate="not-a-bitrate", mastering="fast")
    assert open(out, "rb").read() == before                           # a player never sees a half file
    assert [f for f in os.listdir(tmp_path)] == ["ch.opus"]
