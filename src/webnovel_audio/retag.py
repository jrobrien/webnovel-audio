"""Refresh the Vorbis tags on already-rendered chapters, without re-encoding.

Volume metadata and per-chapter provenance arrived after a lot of audio had
already been produced. Re-synthesising to fix a tag would be absurd — this
remuxes with `-c copy`, which rewrites the tag block and leaves the Opus
bitstream untouched (verified: decoded MD5 is identical before and after).
Roughly 140 ms per chapter.

Note the tags must be set with `-metadata:s:a:0`: in Ogg they live on the
stream, and a plain `-metadata track=N` is silently ignored when the stream
already carries one.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

from . import bundle, sync
from .audio import _ffmpeg
from .config import Config
from .db import DB


def retag_series(cfg: Config, key: str | None = None, *, dry_run: bool = False,
                 log=print) -> dict:
    db = sync._db(cfg)
    done = skipped = failed = 0
    try:
        rows = [db.get_series(key)] if key else db.list_series()
        for s in filter(None, rows):
            slug = sync._dir_slug(s)
            scfg = sync._series_cfg(cfg, slug, bundle.bundle_dir(cfg, s))
            vol_map = db.volume_map(s["id"])
            for c in db.chapters(s["id"]):
                path = c["audio_path"]
                if c["status"] != "rendered" or not path or not os.path.exists(path):
                    skipped += 1
                    continue
                vol = vol_map.get(c["volume_rr_id"]) if c["volume_rr_id"] else None
                tags = _full_tags(scfg, s, c, vol)
                if dry_run:
                    log(f"  would retag #{c['ord'] + 1} {os.path.basename(path)}")
                    done += 1
                    continue
                try:
                    _remux(path, tags)
                    done += 1
                except Exception as exc:  # noqa: BLE001 - one bad file shouldn't stop the run
                    log(f"  ! #{c['ord'] + 1}: {exc}")
                    failed += 1
            log(f"{s['title']}: {done} retagged, {failed} failed")
        return {"retagged": done, "skipped": skipped, "failed": failed}
    finally:
        db.close()


def _full_tags(scfg, s, c, vol) -> dict:
    """Every tag a fresh render would write, reconstructed from the DB.

    Key order mirrors `pipeline.render`: `[metadata]` first, then the
    document-derived fields, so the author wins over the generic
    `[metadata] artist`. SOURCE_URL / FETCHED_AT / RAW_SHA256 normally come
    from the parsed Document; here they're rebuilt from the chapter row and
    the cached raw file, so a retag doesn't drop provenance it can't re-derive.
    """
    tags = dict(scfg.metadata or {})
    tags.update({
        "title": c["title"] or "",
        "album": s["title"] or "",
        "artist": s["author"] or "",
        "comment": c["url"] or "",
        "SOURCE_URL": c["url"] or "",
        "FETCHED_AT": c["fetched_at"] or "",
        "RAW_SHA256": _raw_sha256(c["raw_path"]),
    })
    # the stored render time, not now — this file is not being re-rendered
    tags.update(sync._opus_tags(scfg, s, c, vol, rendered_at=c["rendered_at"]))
    if c["narrator"]:
        tags["PERFORMER"] = c["narrator"]
    return {k: v for k, v in tags.items() if v not in (None, "")}


def _raw_sha256(raw_path: str | None) -> str:
    if not raw_path or not os.path.exists(raw_path):
        return ""
    import hashlib
    with open(raw_path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _remux(path: str, tags: dict) -> None:
    """Rewrite `path`'s tags in place via a temp file, audio untouched.

    `-map_metadata:s:a:0 -1` drops the existing stream tags first; without it
    ffmpeg *merges*, and a re-tagged track number comes out as "69;16".
    """
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(suffix=".opus", dir=d)
    os.close(fd)
    cmd = [_ffmpeg(), "-hide_banner", "-v", "error", "-y", "-i", path, "-c", "copy",
           "-map_metadata", "-1", "-map_metadata:s:a:0", "-1"]
    for k, v in tags.items():
        cmd += ["-metadata:s:a:0", f"{k}={v}"]
    cmd.append(tmp)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        os.replace(tmp, path)          # atomic; never leaves a half-written file
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
