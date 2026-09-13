"""Segment-cache maintenance: what's in it, and how to shrink it.

The cache holds one synthesized wav/flac per `text|voice|style|rate|pitch|
sample_rate`. Nothing ever deleted from it until this module existed, and
lexicon edits orphan keys permanently — 10% of entries were already unreachable
five days in. See `docs/plans/cache-maintenance.md`.

Since the bundle migration each series owns its own cache under
`<bundle>/.cache/`, so "which bytes belong to sky-pride" is a directory
question rather than an inference. Within a bundle, entries live under a
**generation** directory named for the synth fingerprint, so a model or espeak
bump starts a new generation instead of silently reusing the old model's audio.
"""
from __future__ import annotations

import json
import os

from . import bundle
from .config import Config
from .db import DB
from .pipeline import CACHE_EXT, CACHE_SUBTYPE

FINGERPRINT_FILE = ".fingerprint.json"
_AUDIO_EXT = (CACHE_EXT, ".wav")


def current_fingerprint(cfg: Config, backend: str | None = None) -> str:
    """The fingerprint new renders would write under, computed without loading
    the model (hashing the files is ~0.2 s; constructing Kokoro is far more)."""
    backend = backend or cfg.synth.backend
    if backend == "kokoro":
        from .synth.kokoro import fingerprint
        return fingerprint(os.path.expanduser(cfg.general.models_dir),
                           sample_rate=cfg.synth.sample_rate)
    if backend == "null":
        return f"null-{cfg.synth.sample_rate}-2.7"
    return ""


def fingerprint_material(cfg: Config, backend: str | None = None) -> dict:
    backend = backend or cfg.synth.backend
    if backend == "kokoro":
        from .synth.kokoro import fingerprint_material
        return fingerprint_material(os.path.expanduser(cfg.general.models_dir),
                                    sample_rate=cfg.synth.sample_rate)
    return {"backend": backend, "sample_rate": cfg.synth.sample_rate}


def live_keys(cfg: Config, db: DB, series_row=None) -> dict:
    """sha1 -> {slugs referencing it}, replayed from the segment scripts.

    The same reconstruction `pipeline._cache_path` does at write time. This is
    the module's one real hazard: a missing `segments.json` makes live entries
    look orphaned. Per-bundle caches keep the blast radius to one series, and
    `prune` additionally refuses an implausible live set.
    """
    import hashlib
    from .sync import _dir_slug

    rows = [series_row] if series_row is not None else db.list_series()
    out: dict[str, set] = {}
    for s in filter(None, rows):
        slug, bdir = _dir_slug(s), bundle.bundle_dir(cfg, s)
        for d in (os.path.join(bdir, bundle.LAYOUT["chapters"]), bdir):
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if not name.endswith(".segments.json"):
                    continue
                try:
                    with open(os.path.join(d, name), encoding="utf-8") as fh:
                        segs = json.load(fh)
                except (OSError, ValueError):
                    continue
                for seg in segs:
                    if seg.get("kind") != "speech" or not seg.get("text", "").strip():
                        continue
                    mat = (f"{seg['text']}|{seg['voice']}|"
                           f"{seg.get('style', 'narration')}|{seg.get('rate', 1.0)}|"
                           f"{seg.get('pitch', 0.0)}|{cfg.synth.sample_rate}")
                    out.setdefault(hashlib.sha1(mat.encode()).hexdigest(),
                                   set()).add(slug)
    return out


def _entries(root: str):
    """(digest, path, size, generation) for every cached segment under `root`.

    A generation of "" means the flat pre-compact layout.
    """
    if not os.path.isdir(root):
        return
    for backend in sorted(os.listdir(root)):
        bd = os.path.join(root, backend)
        if not os.path.isdir(bd):
            continue
        for name in sorted(os.listdir(bd)):
            p = os.path.join(bd, name)
            if os.path.isdir(p):
                for f in sorted(os.listdir(p)):
                    if f.endswith(_AUDIO_EXT):
                        fp = os.path.join(p, f)
                        yield (f.rsplit(".", 1)[0], fp, os.path.getsize(fp),
                               name, backend)
            elif name.endswith(_AUDIO_EXT):
                yield (name.rsplit(".", 1)[0], p, os.path.getsize(p), "", backend)


def _roots(cfg: Config, db: DB, series_row=None) -> list[tuple[str, str]]:
    """(label, cache root) for every place segments live."""
    from .sync import _dir_slug
    rows = [series_row] if series_row is not None else db.list_series()
    out = [(_dir_slug(s), os.path.join(bundle.bundle_dir(cfg, s),
                                       bundle.LAYOUT["cache"]))
           for s in filter(None, rows)]
    if series_row is None:
        shared = os.path.expanduser(cfg.general.cache_dir)
        if os.path.isdir(shared):
            out.append(("(shared)", shared))
    return out


def status(cfg: Config, db: DB, key: str | None = None) -> dict:
    """What the cache holds, by series and by generation."""
    row = db.get_series(key) if key else None
    if key and not row:
        raise bundle.BundleError("no_such_series",
                                 f"no tracked series matching {key!r}",
                                 hint="webnovel-audio series list")
    live = live_keys(cfg, db, row)
    current = current_fingerprint(cfg)

    series: list[dict] = []
    gens: dict[str, dict] = {}
    fmt = {"flac": 0, "wav": 0}
    tot_files = tot_bytes = 0
    for label, root in _roots(cfg, db, row):
        s_files = s_bytes = s_orphan = s_orphan_bytes = 0
        for digest, path, size, gen, _backend in _entries(root):
            s_files += 1
            s_bytes += size
            fmt["flac" if path.endswith(CACHE_EXT) else "wav"] += 1
            g = gens.setdefault(gen or "(unmigrated)",
                                {"files": 0, "bytes": 0, "current": gen == current})
            g["files"] += 1
            g["bytes"] += size
            owners = live.get(digest)
            reachable = bool(owners) and (label == "(shared)" or label in owners)
            if not reachable or (gen and gen != current):
                s_orphan += 1
                s_orphan_bytes += size
        if s_files:
            series.append({"slug": label, "files": s_files, "bytes": s_bytes,
                           "reclaimable": s_orphan,
                           "reclaimable_bytes": s_orphan_bytes})
        tot_files += s_files
        tot_bytes += s_bytes

    return {
        "ok": True, "current_generation": current,
        "material": fingerprint_material(cfg),
        "files": tot_files, "bytes": tot_bytes,
        "avg_bytes": round(tot_bytes / tot_files) if tot_files else 0,
        "format": fmt, "generations": gens, "series": series,
        "reclaimable": sum(s["reclaimable"] for s in series),
        "reclaimable_bytes": sum(s["reclaimable_bytes"] for s in series),
        "mixed": mixed_generations(db),
    }


def mixed_generations(db: DB) -> list[dict]:
    """Series whose rendered chapters span more than one synth generation.

    A model bump plus a single re-render leaves a book acoustically
    inconsistent mid-way, and nothing else would say so.
    """
    out = []
    for s in db.list_series():
        seen: dict[str, int] = {}
        for c in db.chapters(s["id"]):
            fp = bundle._row_get(c, "synth_fingerprint")
            if c["status"] == "rendered" and fp:
                seen[fp] = seen.get(fp, 0) + 1
        if len(seen) > 1:
            out.append({"slug": s["slug"], "title": s["title"],
                        "generations": seen})
    return out


def compact(cfg: Config, db: DB, key: str | None = None, *, dry_run: bool = False,
            log=print) -> dict:
    """Re-encode float32 wav segments to FLAC and file them under the current
    generation directory.

    Measured on a real 2.1 s segment: 200 KB wav -> 59 KB FLAC/PCM_16, a max
    error of 1.5e-05 (-96 dBFS). That floor sits far below what the ~48 kbps
    Opus encode downstream contributes, and Kokoro output is bounded well under
    unity, so there is no clipping risk.

    The cache **key does not change** — only the container and the parent
    directory — so this never re-synthesizes anything. Each conversion is
    verified before the source is unlinked, making the pass resumable and
    idempotent.
    """
    import soundfile as sf

    row = db.get_series(key) if key else None
    if key and not row:
        raise bundle.BundleError("no_such_series",
                                 f"no tracked series matching {key!r}",
                                 hint="webnovel-audio series list")
    current = current_fingerprint(cfg)
    res = {"converted": 0, "moved": 0, "skipped": 0, "failed": 0,
           "bytes_before": 0, "bytes_after": 0, "dry_run": dry_run}

    for label, root in _roots(cfg, db, row):
        n_before = n_after = conv = moved = 0
        for digest, path, size, gen, backend in list(_entries(root)):
            already_flac = path.endswith(CACHE_EXT)
            in_place = gen == current
            if already_flac and in_place:
                res["skipped"] += 1
                continue
            dst = os.path.join(root, backend, current, f"{digest}{CACHE_EXT}")
            n_before += size
            if dry_run:
                conv += 0 if already_flac else 1
                moved += 1 if already_flac else 0
                n_after += size if already_flac else int(size * 0.3)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                if already_flac:
                    os.replace(path, dst)
                    moved += 1
                else:
                    data, sr = sf.read(path, dtype="float32")
                    sf.write(dst, data, sr, subtype=CACHE_SUBTYPE)
                    sf.info(dst)             # verify before dropping the source
                    os.unlink(path)
                    conv += 1
            except Exception as exc:          # noqa: BLE001 - one bad file, keep going
                log(f"    ! {os.path.basename(path)}: {exc}")
                res["failed"] += 1
                if os.path.exists(dst) and os.path.exists(path):
                    os.unlink(dst)
                continue
            n_after += os.path.getsize(dst)
        res["converted"] += conv
        res["moved"] += moved
        res["bytes_before"] += n_before
        res["bytes_after"] += n_after
        if conv or moved:
            log(f"  {label:<18} {conv:>6} converted, {moved:>5} filed   "
                f"{bundle.human_bytes(n_before)} -> {bundle.human_bytes(n_after)}")
        if not dry_run and (conv or moved):
            _write_fingerprint(cfg, root, current)
        _prune_empty(root)
    return res


def _write_fingerprint(cfg: Config, root: str, current: str) -> None:
    """Record what this generation *is*, so `status` can explain a difference
    in words ("espeak 1.52.0 -> 1.53.0") instead of a changed hash."""
    for backend in sorted(os.listdir(root) if os.path.isdir(root) else []):
        d = os.path.join(root, backend, current)
        if not os.path.isdir(d):
            continue
        p = os.path.join(d, FINGERPRINT_FILE)
        if os.path.exists(p):
            continue
        mat = dict(fingerprint_material(cfg, backend))
        mat["fingerprint"] = current
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(mat, fh, indent=1, sort_keys=True)


def _prune_empty(root: str) -> None:
    for cur, dirs, names in os.walk(root, topdown=False):
        if cur == root or names or dirs:
            continue
        try:
            os.rmdir(cur)
        except OSError:
            pass
