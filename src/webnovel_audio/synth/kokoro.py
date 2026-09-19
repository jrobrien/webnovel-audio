"""Kokoro-82M backend via kokoro-onnx (ONNX Runtime, CPU).

On a Zen 4 chip (AVX-512 / VNNI) this runs comfortably faster than realtime.
Model files are ~350 MB total and are fetched once with `webnovel-audio models fetch`.
"""
from __future__ import annotations

import os

import hashlib

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


#: Things that change what the model *says* or how it says it. Deliberately
#: excludes onnxruntime: a numerics-level dependency, not a semantic one, so
#: including it would churn generations on every routine upgrade for
#: differences below the noise floor.
_FP_PACKAGES = ("kokoro-onnx", "phonemizer", "espeakng-loader")


def fingerprint_material(cache_dir: str = DEFAULT_CACHE, *, lang: str = "en-us",
                         sample_rate: int = 24000) -> dict:
    """What identifies this synthesis generation, expanded.

    Stored beside the cached segments as `.fingerprint.json` so a later
    `cache status` can say *why* two generations differ ("espeak 1.52.0 ->
    1.53.0") rather than just that a hash changed.
    """
    import importlib.metadata as md

    model, voices = model_paths(cache_dir)
    out = {"backend": "kokoro", "lang": lang, "sample_rate": sample_rate,
           "model": _sha256(model)[:16], "voices": _sha256(voices)[:16]}
    for pkg in _FP_PACKAGES:
        try:
            out[pkg] = md.version(pkg)
        except Exception:                       # noqa: BLE001 - absent is fine
            out[pkg] = ""
    return out


def fingerprint(cache_dir: str = DEFAULT_CACHE, *, lang: str = "en-us",
                sample_rate: int = 24000) -> str:
    """A short, stable id for "what produces this audio".

    The segment cache key covers text|voice|style|rate|pitch|sample_rate, which
    misses the model bytes, the voice embeddings, the language, and the g2p
    chain. That last one is the sneaky one: `espeakng-loader` ships its own
    libespeak-ng, so a routine `uv sync` can re-phonemize the whole library
    with no model bump and no visible signal.
    """
    mat = fingerprint_material(cache_dir, lang=lang, sample_rate=sample_rate)
    canon = "|".join(f"{k}={mat[k]}" for k in sorted(mat))
    return hashlib.sha256(canon.encode()).hexdigest()[:12]


def _sha256(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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
        # Kokoro builds the espeak phonemizer here. If the data path is too
        # deep espeak exits the process instead of raising, so this has to
        # refuse *before* that, while there is still something to refuse with.
        from ..espeak import require_usable
        require_usable("cannot render:")
        self._k = Kokoro(model, voices)
        self.default_voice = default_voice
        self.speed = speed
        self.lang = lang
        self.fingerprint = fingerprint(cache_dir, lang=lang,
                                       sample_rate=self.sample_rate)

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
