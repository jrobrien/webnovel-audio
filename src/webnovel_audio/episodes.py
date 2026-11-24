"""Feed episodes that join several rendered chapters into one audio file.

Rendering stays per chapter (state, recipes, lexicon fixes, `render --range`
all work as before); this is a packaging step on top. `[feed]
chapters_per_episode = N` in a series' config.toml groups N consecutive
chapters into `<bundle>/episodes/eNNNN-MMMM.opus`, stream-copied (no
re-encode) with a chapter mark per chapter, which AntennaPod reads from the
Vorbis comments.

Grouping is by position, never by duration, so an episode's membership (and
its feed GUID) cannot drift when a chapter is re-rendered at a slightly
different length. Groups restart at each volume. Only a leading run of
complete groups is published: a hole filled in later would carry an older
date than episodes already in the feed, and podcast apps skip "new episode"
actions for those (see `feed.build_feed`).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field

from . import bundle
from .config import Config
from .package import _duration, _ffmeta, _ffmpeg

#: statuses that leave a chapter out of grouping entirely (a deliberate skip
#: must not hold every later episode back)
_EXCLUDED = ("skipped",)


@dataclass
class Episode:
    index: int                                   # 1-based across the series
    members: list = field(default_factory=list)  # chapter rows, in order
    full: bool = True

    @property
    def first(self):
        return self.members[0]

    @property
    def last(self):
        return self.members[-1]

    @property
    def name(self) -> str:
        return f"e{self.first['ord'] + 1:04d}-{self.last['ord'] + 1:04d}.opus"

    @property
    def title(self) -> str:
        a = self.first["title"] or f"Chapter {self.first['ord'] + 1}"
        if len(self.members) == 1:
            return a
        b = self.last["title"] or f"Chapter {self.last['ord'] + 1}"
        return f"{a} – {b}"

    @property
    def duration_s(self) -> float:
        return sum(float(c["duration_s"] or 0) for c in self.members)

    @property
    def published_at(self) -> str:
        return max((c["published_at"] or "" for c in self.members), default="")


def per_episode(scfg: Config) -> int:
    """The validated `[feed] chapters_per_episode`. A bad value is an error, not a
    silent fallback to 1: it is typed by hand in the config editor."""
    n = scfg.feed.chapters_per_episode
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise SystemExit(f"config: [feed] chapters_per_episode must be a whole number "
                         f">= 1, got {n!r}")
    return n


def _row(c, name, default=None):
    try:
        return c[name] if name in c.keys() else default
    except AttributeError:
        return c.get(name, default)


def _ready(c) -> bool:
    return (c["status"] == "rendered" and bool(c["audio_path"])
            and os.path.exists(c["audio_path"]))


def _old_enough(iso: str, days: int, now: _dt.datetime) -> bool:
    from .feed import _parse_iso
    dt = _parse_iso(iso)
    return dt is not None and (now - dt) >= _dt.timedelta(days=days)


def plan(chapters, n: int, *, partial_after_days: int = 7,
         now: _dt.datetime | None = None) -> list[Episode]:
    """The episodes to publish: a leading run of groups whose chapters are all
    rendered. A short group is published only when something follows it (its
    volume ended) or its newest chapter is `partial_after_days` old."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    rows = sorted((c for c in chapters if c["status"] not in _EXCLUDED),
                  key=lambda c: c["ord"])
    groups: list[list] = []
    for c in rows:
        vol = _row(c, "volume_source_id")
        if (groups and len(groups[-1]) < n
                and _row(groups[-1][-1], "volume_source_id") == vol):
            groups[-1].append(c)
        else:
            groups.append([c])
    out: list[Episode] = []
    for i, g in enumerate(groups):
        if not all(_ready(c) for c in g):
            break
        full = len(g) == n
        if not full and i == len(groups) - 1:
            newest = max((c["published_at"] or "" for c in g), default="")
            if not _old_enough(newest, partial_after_days, now):
                break
        out.append(Episode(index=i + 1, members=list(g), full=full))
    return out


def _dir(bdir: str) -> str:
    return os.path.join(bdir, bundle.LAYOUT["episodes"])


def _fingerprint(ep: Episode) -> list:
    """What the episode file was made from; a re-rendered chapter changes it."""
    out = []
    for c in ep.members:
        st = os.stat(c["audio_path"])
        out.append([os.path.basename(c["audio_path"]), st.st_size, st.st_mtime_ns,
                    c["title"] or ""])
    return out


def _sidecar(path: str) -> str:
    return path[:-len(".opus")] + ".json"


def _stamp(ep: Episode, series_row, vol_title: str = "") -> dict:
    """What the episode file was made from: its chapters' audio, and the
    series-level values written into its tags (a corrected author rebuilds it)."""
    return {"index": ep.index, "members": _fingerprint(ep),
            "tags": [series_row["title"], _row(series_row, "author") or "", vol_title]}


def _fresh(path: str, ep: Episode, series_row=None, vol_title: str = "") -> bool:
    try:
        with open(_sidecar(path), encoding="utf-8") as fh:
            have = json.load(fh)
    except (OSError, ValueError):
        return False
    if not os.path.exists(path) or have.get("members") != _fingerprint(ep):
        return False
    return series_row is None or have.get("tags") == _stamp(ep, series_row, vol_title)["tags"]


def _write(ep: Episode, path: str, series_row, vol_title: str = "") -> None:
    marks, cursor = [], 0
    for c in ep.members:
        dur = _duration(c["audio_path"]) or float(c["duration_s"] or 0)
        ms = max(1, int(round(dur * 1000)))
        marks.append((cursor, cursor + ms, c["title"] or f"Chapter {c['ord'] + 1}"))
        cursor += ms
    album = series_row["title"] + (f" — {vol_title}" if vol_title else "")
    meta = _ffmeta(ep.title, _row(series_row, "author") or "webnovel-audio", marks)
    # global keys must precede the first [CHAPTER] section, or they land in it
    head, sep, chapters = meta.partition("[CHAPTER]")
    head = head.replace(f"album={ep.title}", f"album={album}", 1) + \
        f"genre=Audiobook\ntrack={ep.index}\n" + \
        (f"date={ep.published_at[:10]}\n" if ep.published_at else "")
    meta = head + sep + chapters
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with tempfile.TemporaryDirectory(dir=os.path.dirname(path)) as td:
        listfile = os.path.join(td, "list.txt")
        with open(listfile, "w", encoding="utf-8") as fh:
            for c in ep.members:
                p = os.path.abspath(c["audio_path"]).replace("'", "'\\''")
                fh.write(f"file '{p}'\n")
        metafile = os.path.join(td, "meta.txt")
        with open(metafile, "w", encoding="utf-8") as fh:
            fh.write(meta)
        tmp = os.path.join(td, "out.opus")
        # stream tags come from the metadata file only (`-map_metadata:s:a`): the
        # first chapter's own title/source URL would otherwise label the episode.
        # Not `-map_metadata -1`: that also drops the chapter titles.
        subprocess.run([_ffmpeg(), "-hide_banner", "-nostats", "-loglevel", "error", "-y",
                        "-f", "concat", "-safe", "0", "-i", listfile, "-i", metafile,
                        "-map", "0:a", "-map_metadata:s:a", "1:g",
                        "-map_chapters", "1", "-c", "copy", tmp],
                       check=True, capture_output=True, text=True)
        os.replace(tmp, path)
    with open(_sidecar(path), "w", encoding="utf-8") as fh:
        json.dump(_stamp(ep, series_row, vol_title), fh, indent=1)


def _series_view(cfg: Config, db, series_row):
    from .sync import _dir_slug, _series_cfg
    bdir = bundle.bundle_dir(cfg, series_row)
    return bdir, _series_cfg(cfg, _dir_slug(series_row), bdir)


def build(cfg: Config, db, series_row, log=print) -> dict:
    """Bring `<bundle>/episodes/` in line with the plan: (re)build what is
    missing or stale, delete files no longer in it. With chapters_per_episode
    = 1 that empties the directory, and the feed goes back to one per chapter."""
    bdir, scfg = _series_view(cfg, db, series_row)
    n = per_episode(scfg)
    eps = plan(db.chapters(series_row["id"]), n,
               partial_after_days=scfg.feed.partial_after_days) if n > 1 else []
    d = _dir(bdir)
    vols = db.volume_map(series_row["id"])
    built = kept = removed = 0
    wanted = set()
    for ep in eps:
        path = os.path.join(d, ep.name)
        wanted |= {os.path.basename(path), os.path.basename(_sidecar(path))}
        vol = vols.get(_row(ep.first, "volume_source_id")) or {}
        if _fresh(path, ep, series_row, vol.get("title", "")):
            kept += 1
            continue
        _write(ep, path, series_row, vol.get("title", ""))
        built += 1
        log(f"  episode {ep.index}: {ep.name} ({len(ep.members)} chapters)")
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f not in wanted and f.endswith((".opus", ".json")):
                os.remove(os.path.join(d, f))
                removed += f.endswith(".opus")
    return {"slug": series_row["slug"], "per_episode": n, "episodes": len(eps),
            "built": built, "kept": kept, "removed": removed}


def for_feed(cfg: Config, db, series_row) -> list[Episode] | None:
    """The episodes the feed should list, or None for one item per chapter.

    Read-only: lists only episode files that `build` has already written (a
    feed request must never run ffmpeg). A chapter re-rendered since then keeps
    its old episode file until the next build; that is still valid audio."""
    bdir, scfg = _series_view(cfg, db, series_row)
    try:
        n = per_episode(scfg)
    except SystemExit:
        return None
    if n == 1:
        return None
    eps = plan(db.chapters(series_row["id"]), n,
               partial_after_days=scfg.feed.partial_after_days)
    out = []
    for ep in eps:
        path = os.path.join(_dir(bdir), ep.name)
        if not os.path.exists(path):
            break                                   # keep the published run contiguous
        ep.path = path                              # type: ignore[attr-defined]
        out.append(ep)
    return out
