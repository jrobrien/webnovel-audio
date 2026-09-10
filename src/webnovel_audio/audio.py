"""Assembly + mastering: concatenate segment renders, insert pauses,
two-pass loudness-normalize, encode to Opus."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import numpy as np
import soundfile as sf


def silence(ms: int, sr: int) -> np.ndarray:
    return np.zeros(max(0, int(sr * ms / 1000)), dtype="float32")


def _shape(x: np.ndarray, sr: int, hp_hz, lp_hz, tilt_db) -> np.ndarray:
    """FFT-domain soft one-pole HP/LP plus an optional spectral tilt (dB/octave)."""
    if x.size < 32:
        return x
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(x.size, 1.0 / sr)
    if hp_hz:
        spec *= (f / hp_hz) ** 2 / (1.0 + (f / hp_hz) ** 2)
    if lp_hz:
        spec *= 1.0 / (1.0 + (f / lp_hz) ** 2)
    if tilt_db:
        octaves = np.log2(np.maximum(f, 1.0) / 1000.0)
        spec *= 10.0 ** (tilt_db * octaves / 20.0)
    return np.fft.irfft(spec, n=x.size).astype("float32")


def _time_stretch(x: np.ndarray, rate: float, frame: int = 1024, hop_out: int = 256) -> np.ndarray:
    """Overlap-add time stretch by `rate` (>1 = longer). Dependency-free, mild artefacts."""
    if abs(rate - 1.0) < 1e-3 or x.size < frame * 2:
        return x
    hop_in = max(1, int(round(hop_out / rate)))
    win = np.hanning(frame).astype("float32")
    n = 1 + (x.size - frame) // hop_in
    out = np.zeros(frame + hop_out * n, dtype="float32")
    norm = np.zeros_like(out)
    for i in range(n):
        a = i * hop_in
        seg = x[a:a + frame]
        if seg.size < frame:
            seg = np.pad(seg, (0, frame - seg.size))
        o = i * hop_out
        out[o:o + frame] += seg * win
        norm[o:o + frame] += win
    norm[norm < 1e-6] = 1.0
    return out / norm


def pitch_shift(x: np.ndarray, sr: int, semitones: float) -> np.ndarray:
    """Shift pitch, keeping duration: OLA stretch then linear-resample back."""
    semitones = max(-6.0, min(6.0, semitones))
    if abs(semitones) < 0.05 or x.size < 2048:
        return x.astype("float32")
    factor = 2.0 ** (semitones / 12.0)
    stretched = _time_stretch(x, factor)
    idx = np.arange(x.size) * (stretched.size / x.size)
    lo = np.floor(idx).astype(int)
    lo = np.clip(lo, 0, stretched.size - 2)
    frac = (idx - lo).astype("float32")
    return (stretched[lo] * (1.0 - frac) + stretched[lo + 1] * frac).astype("float32")


def apply_chain(x: np.ndarray, sr: int, spec: dict) -> np.ndarray:
    """Run one effect chain: {semitones, hp_hz, lp_hz, tilt_db, gain_db}."""
    if x is None or x.size == 0 or not spec:
        return x
    st = float(spec.get("semitones", 0.0) or 0.0)
    if abs(st) >= 0.05:
        x = pitch_shift(x, sr, st)
    if spec.get("hp_hz") or spec.get("lp_hz") or spec.get("tilt_db"):
        x = _shape(x, sr, spec.get("hp_hz"), spec.get("lp_hz"), spec.get("tilt_db"))
    g = float(spec.get("gain_db", 0.0) or 0.0)
    if g:
        x = x * (10.0 ** (g / 20.0))
    return x.astype("float32")


def inner_voice(x: np.ndarray, sr: int) -> np.ndarray:
    """Back-compat: the default 'thought' chain."""
    return apply_chain(x, sr, {"gain_db": -1.5, "hp_hz": 115.0, "lp_hz": 3600.0})


def earcon(sr: int, *, level: float = 0.11) -> np.ndarray:
    """A short, soft two-pip cue marking the start of a chat run."""
    def pip(freq: float, ms: int) -> np.ndarray:
        n = int(sr * ms / 1000)
        t = np.arange(n) / sr
        env = np.sin(np.pi * np.linspace(0, 1, n, endpoint=False)) ** 2
        return (np.sin(2 * np.pi * freq * t) * env).astype("float32")

    gap = np.zeros(int(sr * 0.02), dtype="float32")
    sig = np.concatenate([pip(1180.0, 55), gap, pip(1560.0, 65)])
    return _shape(sig, sr, 240.0, 5200.0, 0.0) * level


def assemble(segments, renders, sr: int, *, lead_ms: int = 400, tail_ms: int = 900) -> np.ndarray:
    parts: list[np.ndarray] = [silence(lead_ms, sr)]
    for seg, audio in zip(segments, renders):
        if audio is not None and getattr(audio, "size", 0):
            parts.append(np.asarray(audio, dtype="float32"))
        if seg.pause_after_ms:
            parts.append(silence(seg.pause_after_ms, sr))
    parts.append(silence(tail_ms, sr))
    wav = np.concatenate(parts) if parts else np.zeros(0, dtype="float32")
    peak = float(np.max(np.abs(wav))) if wav.size else 0.0
    if peak > 1.0:
        wav = wav / peak
    return wav


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SystemExit("ffmpeg not found on PATH")
    return exe


def _all_finite(d: dict, *keys: str) -> bool:
    try:
        return all(-1e6 < float(d[k]) < 1e6 for k in keys)
    except (KeyError, TypeError, ValueError):
        return False


def measure_loudnorm(in_wav: str, i: float, tp: float, lra: float) -> dict | None:
    cmd = [
        _ffmpeg(), "-hide_banner", "-nostats", "-i", in_wav,
        "-af", f"loudnorm=I={i}:TP={tp}:LRA={lra}:print_format=json",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    text = proc.stderr
    start, end = text.rfind("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _ffmeta_chapters(chapters, total_s: float) -> str:
    """An ffmetadata document with one [CHAPTER] block per (start_seconds, title)."""
    lines = [";FFMETADATA1"]
    marks = sorted(chapters, key=lambda c: c[0])
    for n, (start, title) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else total_s
        safe = str(title).replace("\\", "\\\\").replace("\n", " ").replace("=", "-")
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={int(round(start * 1000))}",
                  f"END={int(round(max(end, start) * 1000))}",
                  f"title={safe}"]
    return "\n".join(lines) + "\n"


def write_opus(
    wav: np.ndarray,
    sr: int,
    out_path: str,
    *,
    bitrate: str = "56k",
    loud: tuple[float, float, float] = (-19.0, -3.0, 11.0),
    meta: dict | None = None,
    chapters: list[tuple[float, str]] | None = None,
) -> None:
    meta = meta or {}
    i, tp, lra = loud
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, "raw.wav")
        sf.write(raw, wav, sr, subtype="PCM_16")

        af = f"loudnorm=I={i}:TP={tp}:LRA={lra}"
        m = measure_loudnorm(raw, i, tp, lra)
        if m and _all_finite(m, "input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
            af += (
                f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
                f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
                f":offset={m['target_offset']}:linear=true"
            )

        cmd = [_ffmpeg(), "-hide_banner", "-nostats", "-y", "-i", raw]
        if chapters:
            fm = os.path.join(td, "chapters.txt")
            with open(fm, "w", encoding="utf-8") as fh:
                fh.write(_ffmeta_chapters(chapters, wav.size / sr))
            cmd += ["-i", fm, "-map", "0:a", "-map_chapters", "1"]
        cmd += [
            "-af", af, "-ar", "48000", "-ac", "1",
            "-c:a", "libopus", "-b:a", bitrate, "-vbr", "on",
        ]
        for key, value in meta.items():
            cmd += ["-metadata", f"{key}={value}"]
        cmd.append(out_path)
        subprocess.run(cmd, check=True, capture_output=True, text=True)
