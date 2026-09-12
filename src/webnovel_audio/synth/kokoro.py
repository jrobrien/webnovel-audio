"""Kokoro-82M backend via kokoro-onnx (ONNX Runtime, CPU).

On a Zen 4 chip (AVX-512 / VNNI) this runs comfortably faster than realtime.
Model files are ~350 MB total and are fetched once with `webnovel-audio models fetch`.
"""
from __future__ import annotations

import os

import numpy as np

from ..segment import Segment

DEFAULT_CACHE = os.path.expanduser("~/.cache/webnovel-audio")
_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"
MODEL_URL = f"{_RELEASE}/{MODEL_FILE}"
VOICES_URL = f"{_RELEASE}/{VOICES_FILE}"


def model_paths(cache_dir: str = DEFAULT_CACHE) -> tuple[str, str]:
    return os.path.join(cache_dir, MODEL_FILE), os.path.join(cache_dir, VOICES_FILE)


def fetch_models(cache_dir: str = DEFAULT_CACHE, log=print) -> None:
    import urllib.request

    os.makedirs(cache_dir, exist_ok=True)
    for url, name in ((MODEL_URL, MODEL_FILE), (VOICES_URL, VOICES_FILE)):
        dst = os.path.join(cache_dir, name)
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            log(f"  have  {name} ({os.path.getsize(dst) / 1e6:.0f} MB)")
            continue
        log(f"  fetch {name} ...")
        tmp = dst + ".part"
        urllib.request.urlretrieve(url, tmp)
        os.replace(tmp, dst)
        log(f"  saved {dst} ({os.path.getsize(dst) / 1e6:.0f} MB)")


def _resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out or x.size == 0:
        return x
    n_out = int(round(x.size * sr_out / sr_in))
    src = np.linspace(0.0, 1.0, x.size, endpoint=False)
    dst = np.linspace(0.0, 1.0, n_out, endpoint=False)
    return np.interp(dst, src, x).astype("float32")


class KokoroSynth:
    sample_rate = 24000

    def __init__(
        self,
        default_voice: str = "am_michael",
        cache_dir: str = DEFAULT_CACHE,
        speed: float = 1.0,
        lang: str = "en-us",
    ):
        try:
            from kokoro_onnx import Kokoro
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise SystemExit(
                "kokoro-onnx is not installed.  ->  uv sync --extra kokoro"
            ) from exc

        model, voices = model_paths(cache_dir)
        if not (os.path.exists(model) and os.path.exists(voices)):
            raise SystemExit(
                "Kokoro model files missing.  ->  webnovel-audio models fetch"
            )
        self._k = Kokoro(model, voices)
        self.default_voice = default_voice
        self.speed = speed
        self.lang = lang

    def synth(self, seg: Segment) -> np.ndarray:
        if not seg.text.strip():
            return np.zeros(0, dtype="float32")
        samples, sr = self._k.create(
            seg.text,
            voice=seg.voice or self.default_voice,
            speed=seg.rate or self.speed,
            lang=self.lang,
        )
        samples = np.asarray(samples, dtype="float32")
        return _resample(samples, sr, self.sample_rate)

    def close(self) -> None:
        pass
