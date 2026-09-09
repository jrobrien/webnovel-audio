"""Stitch rendered chapters into a single chapterised .m4b audiobook."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SystemExit("ffmpeg not found on PATH")
    return exe


def _duration(path: str) -> float:
    exe = shutil.which("ffprobe")
    if not exe:
        return 0.0
    out = subprocess.run(
        [exe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def _ffmeta(title: str, author: str, marks: list[tuple[int, int, str]]) -> str:
    lines = [";FFMETADATA1", f"title={title}", f"artist={author}", f"album={title}"]
    for start_ms, end_ms, chap_title in marks:
        safe = chap_title.replace("\n", " ").replace("=", "-").replace(";", ",")
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={start_ms}", f"END={end_ms}", f"title={safe}"]
    return "\n".join(lines) + "\n"


def build_m4b(chapters, out_path: str, *, title: str, author: str = "",
              cover_path: str | None = None, bitrate: str = "64k", log=print) -> str:
    """`chapters`: rows (dicts) with audio_path / title / ord / duration_s, in order."""
    tracks = [c for c in chapters
              if c["status"] == "rendered" and c["audio_path"]
              and os.path.exists(c["audio_path"])]
    if not tracks:
        raise SystemExit("no rendered chapters to package")

    marks: list[tuple[int, int, str]] = []
    cursor = 0
    for c in tracks:
        dur = c["duration_s"] or _duration(c["audio_path"])
        dur_ms = max(1, int(round(dur * 1000)))
        marks.append((cursor, cursor + dur_ms, c["title"] or f"Chapter {c['ord'] + 1}"))
        cursor += dur_ms

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        listfile = os.path.join(td, "list.txt")
        with open(listfile, "w", encoding="utf-8") as fh:
            for c in tracks:
                p = os.path.abspath(c["audio_path"]).replace("'", "'\\''")
                fh.write(f"file '{p}'\n")
        metafile = os.path.join(td, "meta.txt")
        with open(metafile, "w", encoding="utf-8") as fh:
            fh.write(_ffmeta(title, author or "webnovel-audio", marks))

        cmd = [_ffmpeg(), "-hide_banner", "-nostats", "-y",
               "-f", "concat", "-safe", "0", "-i", listfile,
               "-i", metafile]
        if cover_path and os.path.exists(cover_path):
            cmd += ["-i", cover_path]
        cmd += ["-map_metadata", "1", "-map_chapters", "1", "-map", "0:a"]
        if cover_path and os.path.exists(cover_path):
            cmd += ["-map", "2:v", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
        cmd += ["-c:a", "aac", "-b:a", bitrate, "-ac", "1",
                "-movflags", "+faststart", "-f", "mp4", out_path]
        log(f"packaging {len(tracks)} chapters -> {out_path}")
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out_path
