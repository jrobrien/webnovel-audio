"""A series bundle: the on-disk directory that describes one series.

Three files, deliberately split by *who writes them*:

  manifest.toml   identity + provenance   machine-written, hand edits lost
  state.json      per-chapter state       machine-written, rewritten each sync
  config.toml     the render overlay      hand-edited (and by `cast set`)
  lexicon.csv     pronunciations          hand-edited

`config.toml` and `lexicon.csv` still live under `data/` today; this module
writes the two machine-owned files into the existing `library/<slug>/` layout
so the format can be reviewed on real data before anything moves. See
`docs/plans/series-bundles.md`.

Authority rule: **while a bundle is tracked, the state DB wins.** `state.json`
is an export, authoritative only at `import`, where the DB has nothing to say.
A sync that dies mid-write therefore leaves a stale export, not a split brain.
"""
from __future__ import annotations

import json
import os
import time
import tomllib
import uuid as _uuid

from .config import Config, resolve_data_path
from .db import DB

SCHEMA = 1
MANIFEST = "manifest.toml"
STATE = "state.json"

# Columns exported verbatim. Listed rather than `SELECT *`-ed so a new column
# has to be considered here too, instead of silently failing to round-trip.
_CHAPTER_COLS = (
    "rr_id", "ord", "title", "slug", "url", "published_at", "unlocked",
    "status", "error_stage", "raw_path", "text_path", "audio_path",
    "duration_s", "fetched_at", "parsed_at", "rendered_at",
    "render_started_at", "render_ended_at", "volume_rr_id", "volume_chapter",
    "narrator", "synth_fingerprint", "error",
)
_VOLUME_COLS = ("rr_id", "title", "cover_url", "ord")
_SERIES_COLS = ("rr_id", "provider", "slug", "title", "author", "url",
                "cover_url", "enabled", "tags", "warnings", "status", "rating",
                "added_at", "uuid")

# Paths in these columns are rewritten bundle-relative on export and back on
# import, so a bundle can be moved or unpacked anywhere.
_PATH_COLS = ("raw_path", "text_path", "audio_path")


def _row_get(row, name, default=None):
    """sqlite3.Row has no .get(), and old DBs may lack a column entirely."""
    try:
        keys = row.keys()
    except AttributeError:
        return row.get(name, default)
    return row[name] if name in keys else default


def bundle_dir(cfg: Config, series_row) -> str:
    """Where this series' bundle lives.

    Prefers the recorded `series.path` so that everything already resolves
    through it — when the files actually move (plan step 3) only the recorded
    path changes, not the callers.
    """
    p = _row_get(series_row, "path")
    if p:
        return os.path.abspath(os.path.expanduser(p))
    from .sync import _dir_slug
    return os.path.abspath(os.path.join(
        os.path.expanduser(cfg.royalroad.library_dir), _dir_slug(series_row)))


#: every per-series path, relative to the bundle root — the single place that
#: knows the layout
LAYOUT = {
    "manifest": MANIFEST,
    "state": STATE,
    "config": "config.toml",
    "lexicon": "lexicon.csv",
    "chapters": "chapters",
    "covers": "covers",
    "raw": ".raw",
    "cache": ".cache",
}


def paths(bdir: str) -> dict:
    """Resolve `LAYOUT` against a bundle directory."""
    return {k: os.path.join(bdir, v) for k, v in LAYOUT.items()} | {"dir": bdir}


def legacy_paths(cfg: Config, slug: str) -> dict:
    """Where these files lived before the bundle migration. Read-only fallback,
    so an un-migrated tree keeps working until `migrate bundles` runs."""
    lib = os.path.expanduser(cfg.royalroad.library_dir)
    return {
        # Anchored to the checkout, not the working directory -- same reason
        # as the base lexicon (see config.resolve_data_path). Harmless on a
        # migrated tree, where `resolve` prefers the bundle anyway; on an
        # un-migrated one it is the difference between finding the legacy
        # file and silently deciding there isn't one.
        "config": os.path.join(
            resolve_data_path(cfg.general.series_config_dir or "data/series"),
            f"{slug}.toml"),
        "lexicon": os.path.join(
            resolve_data_path(cfg.general.lexicon_dir or "data/lexicons"),
            f"{slug}.csv"),
        "chapters": os.path.join(lib, slug),
        "covers": os.path.join(lib, slug),
        "raw": os.path.join(lib, slug, ".raw"),
        "cache": os.path.expanduser(cfg.general.cache_dir),
    }


def resolve(cfg: Config, slug: str, bdir: str, which: str) -> str:
    """A bundle path, falling back to the legacy location if the bundle has no
    such file yet. Writers should use `paths()` directly; this is for readers
    that must tolerate a half-migrated tree."""
    p = os.path.join(bdir, LAYOUT[which])
    if os.path.exists(p):
        return p
    legacy = legacy_paths(cfg, slug).get(which, "")
    return legacy if legacy and os.path.exists(legacy) else p


def artifact(bdir: str, which: str, name: str) -> str:
    """A named file inside the bundle, tolerating the pre-migration flat
    layout where chapters and covers sat directly in the series directory."""
    p = os.path.join(bdir, LAYOUT[which], os.path.basename(name))
    return p if os.path.exists(p) else os.path.join(bdir, os.path.basename(name))


def exists(cfg: Config, series_row) -> bool:
    return os.path.isdir(bundle_dir(cfg, series_row))


def status(cfg: Config, series_row) -> str:
    """ok | missing | bare — what the tracked path actually holds.

    `missing` is a supported state, not an error: deleting a series with
    `rm -rf` is a legitimate way to reclaim space in a hurry, and every command
    has to survive it.
    """
    d = bundle_dir(cfg, series_row)
    if not os.path.isdir(d):
        return "missing"
    return "ok" if os.path.isfile(os.path.join(d, MANIFEST)) else "bare"


# -- writing ---------------------------------------------------------------

def _toml_str(s) -> str:
    return '"' + str(s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_list(xs) -> str:
    return "[" + ", ".join(_toml_str(x) for x in (xs or [])) + "]"


def _jlist(row, col):
    raw = _row_get(row, col)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _sha256_file(path: str) -> str:
    import hashlib
    if not path or not os.path.isfile(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(cfg: Config, series_row, *, fingerprints=None) -> str:
    """Identity + provenance. Rewritten whenever the series is synced; the
    hashes are what later warns that a bundle was rendered against a different
    base lexicon than the one now on disk."""
    d = bundle_dir(cfg, series_row)
    uid = _row_get(series_row, "uuid") or str(_uuid.uuid4())
    base_lex = resolve_data_path(cfg.general.base_lexicon)
    lines = [
        "# manifest.toml — written by webnovel-audio; hand edits will be overwritten.",
        "# Identity and provenance only. Render settings live in config.toml.",
        "",
        f"schema = {SCHEMA}",
        f"uuid   = {_toml_str(uid)}",
        f"slug   = {_toml_str(_row_get(series_row, 'slug'))}",
        "",
        "[source]",
        f"provider = {_toml_str(_row_get(series_row, 'provider') or 'royalroad')}",
        f"id       = {_toml_str(_row_get(series_row, 'rr_id'))}",
        f"url      = {_toml_str(_row_get(series_row, 'url'))}",
        f"title    = {_toml_str(_row_get(series_row, 'title'))}",
        f"author   = {_toml_str(_row_get(series_row, 'author'))}",
        f"added_at = {_toml_str(_row_get(series_row, 'added_at'))}",
        "",
        "[provenance]",
        f"last_synced_at      = {_toml_str(time.strftime('%Y-%m-%dT%H:%M:%S'))}",
        f"base_lexicon_sha256 = {_toml_str(_sha256_file(base_lex))}",
        f"synth_fingerprints  = {_toml_list(fingerprints)}",
        "",
    ]
    path = os.path.join(d, MANIFEST)
    _atomic_write(path, "\n".join(lines))
    return path


def export_state(cfg: Config, db: DB, series_row) -> str:
    """Dump this series' rows to `state.json`.

    This is what makes `series import` exact. Inferring state from the files on
    disk recovers most of it, but loses `render_started_at`/`render_ended_at`
    (the render-cost model's only input) and cannot recover `published_at`,
    `volume_rr_id` or `unlocked` without going back to the network.
    """
    d = bundle_dir(cfg, series_row)
    sid = series_row["id"]

    def chapter(c):
        out = {k: _row_get(c, k) for k in _CHAPTER_COLS}
        for k in _PATH_COLS:
            out[k] = _relpath(out[k], d)
        return out

    doc = {
        "schema": SCHEMA,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "series": {k: _row_get(series_row, k) for k in _SERIES_COLS},
        "volumes": [{k: _row_get(v, k) for k in _VOLUME_COLS}
                    for v in db.volumes(sid)],
        "chapters": [chapter(c) for c in db.chapters(sid)],
    }
    path = os.path.join(d, STATE)
    _atomic_write(path, json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    return path


def _relpath(p: str | None, base: str) -> str | None:
    """Bundle-relative, so the directory can be moved or unpacked anywhere."""
    if not p:
        return p
    ap = os.path.abspath(os.path.expanduser(p))
    base = os.path.abspath(base)
    if ap == base or ap.startswith(base + os.sep):
        return os.path.relpath(ap, base)
    return p            # outside the bundle: leave alone rather than invent one


def _abspath(p: str | None, base: str) -> str | None:
    if not p or os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(base, p))


def sync_bundle(cfg: Config, db: DB, series_row, *, fingerprints=None) -> dict:
    """Write both machine-owned files and record the location. Safe to call
    often; cheap (one small TOML + one JSON)."""
    d = bundle_dir(cfg, series_row)
    if not os.path.isdir(d):
        return {"ok": False, "reason": "missing", "path": d}
    uid = _row_get(series_row, "uuid") or str(_uuid.uuid4())
    db.set_bundle(series_row["id"], d, uid)
    series_row = db.get_series(str(_row_get(series_row, "rr_id")))
    return {"ok": True, "path": d,
            "manifest": write_manifest(cfg, series_row, fingerprints=fingerprints),
            "state": export_state(cfg, db, series_row)}


# -- reading ---------------------------------------------------------------

class BundleError(Exception):
    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint


def read_manifest(path: str) -> dict:
    """`path` may be the bundle directory or the manifest itself."""
    if os.path.isdir(path):
        path = os.path.join(path, MANIFEST)
    if not os.path.isfile(path):
        raise BundleError("not_a_bundle", f"no {MANIFEST} in {os.path.dirname(path)!r}",
                          hint="webnovel-audio series scan <root>")
    try:
        with open(path, "rb") as fh:
            doc = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise BundleError("bad_manifest", f"{path}: {exc}") from exc
    if int(doc.get("schema", 0)) > SCHEMA:
        raise BundleError(
            "future_schema",
            f"{path}: schema {doc.get('schema')} is newer than this build "
            f"understands ({SCHEMA})")
    if not doc.get("uuid"):
        raise BundleError("bad_manifest", f"{path}: no uuid")
    return doc


def read_state(path: str) -> dict:
    if os.path.isdir(path):
        path = os.path.join(path, STATE)
    if not os.path.isfile(path):
        raise BundleError("no_state", f"no {STATE} beside the manifest",
                          hint="webnovel-audio series refresh <key>")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def import_bundle(cfg: Config, db: DB, path: str, *, dry_run: bool = False) -> dict:
    """Adopt a bundle into the state DB, replaying `state.json`.

    Matching is by uuid first, then by (provider, source id) so that a bundle
    copied from elsewhere recognises a series already tracked here instead of
    duplicating it.
    """
    d = os.path.abspath(os.path.expanduser(path))
    man = read_manifest(d)
    src = man.get("source", {})
    uid, rr_id = man["uuid"], str(src.get("id") or "")
    st = read_state(d)

    existing = db.by_uuid(uid) or (db.get_series(rr_id) if rr_id else None)
    action = "update" if existing else "add"
    chapters = st.get("chapters", [])
    if dry_run:
        return {"ok": True, "action": action, "dry_run": True, "path": d,
                "uuid": uid, "slug": man.get("slug"), "title": src.get("title"),
                "chapters": len(chapters), "volumes": len(st.get("volumes", []))}

    sid = _upsert_from_state(db, st, uid, existing)
    db.set_bundle(sid, d, uid)
    _replay_chapters(db, sid, chapters, d)
    _replay_volumes(db, sid, st.get("volumes", []))
    return {"ok": True, "action": action, "path": d, "uuid": uid,
            "slug": man.get("slug"), "title": src.get("title"),
            "chapters": len(chapters), "volumes": len(st.get("volumes", []))}


def _upsert_from_state(db: DB, st: dict, uid: str, existing) -> int:
    s = st.get("series", {})
    cols = [c for c in _SERIES_COLS if c != "uuid"]
    vals = [s.get(c) for c in cols]
    if existing:
        db.con.execute(
            f"UPDATE series SET {', '.join(f'{c}=?' for c in cols)}, uuid=? WHERE id=?",
            (*vals, uid, existing["id"]))
        db.con.commit()
        return existing["id"]
    cur = db.con.execute(
        f"INSERT INTO series ({', '.join(cols)}, uuid) "
        f"VALUES ({', '.join('?' * len(cols))}, ?)", (*vals, uid))
    db.con.commit()
    return cur.lastrowid


def _replay_chapters(db: DB, sid: int, chapters: list[dict], base: str) -> None:
    cols = list(_CHAPTER_COLS)
    sets = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "rr_id")
    for c in chapters:
        row = dict(c)
        for k in _PATH_COLS:
            row[k] = _abspath(row.get(k), base)
        db.con.execute(
            f"INSERT INTO chapters (series_id, {', '.join(cols)}) "
            f"VALUES (?, {', '.join('?' * len(cols))}) "
            f"ON CONFLICT(series_id, rr_id) DO UPDATE SET {sets}",
            (sid, *[row.get(k) for k in cols]))
    db.con.commit()


def _replay_volumes(db: DB, sid: int, volumes: list[dict]) -> None:
    for v in volumes:
        db.con.execute(
            """INSERT INTO volumes (series_id, rr_id, title, cover_url, ord)
               VALUES (?,?,?,?,?)
               ON CONFLICT(series_id, rr_id) DO UPDATE SET
                 title=excluded.title, cover_url=excluded.cover_url,
                 ord=excluded.ord""",
            (sid, str(v.get("rr_id")), v.get("title"), v.get("cover_url"),
             v.get("ord")))
    db.con.commit()


def migrate(cfg: Config, db: DB, key: str | None = None, *,
            dry_run: bool = False, log=print) -> list[dict]:
    """Move a series into the bundle layout: chapters/, covers/, config.toml,
    lexicon.csv, and its share of the segment cache.

    Idempotent — a bundle already migrated is skipped. Every move is a rename
    or a hard link within one filesystem, so nothing is copied and the DB path
    rewrite is the only thing that can leave a mismatch.
    """
    from .sync import _dir_slug
    rows = [db.get_series(key)] if key else db.list_series()
    out: list[dict] = []
    live = _live_keys(cfg, db)
    for s in filter(None, rows):
        if not s:
            continue
        slug, bdir = _dir_slug(s), bundle_dir(cfg, s)
        res = {"slug": slug, "title": s["title"], "path": bdir,
               "moved": 0, "cache_linked": 0, "skipped": False}
        if not os.path.isdir(bdir):
            res["skipped"] = "missing"
            out.append(res)
            continue
        p = paths(bdir)
        legacy = legacy_paths(cfg, slug)

        moves: list[tuple[str, str]] = []
        # chapter artefacts: NNN-*.md / .opus / .segments.json
        for name in sorted(os.listdir(bdir)):
            src = os.path.join(bdir, name)
            if not os.path.isfile(src):
                continue
            if name[:3].isdigit() and name[3:4] == "-":
                moves.append((src, os.path.join(p["chapters"], name)))
            elif name == "cover.jpg" or name.startswith("cover-v"):
                moves.append((src, os.path.join(p["covers"], name)))
        # the two hand-edited files, from their old shared directories
        for which in ("config", "lexicon"):
            if os.path.isfile(legacy[which]) and not os.path.exists(p[which]):
                moves.append((legacy[which], p[which]))

        res["moved"] = len(moves)
        if not dry_run:
            for src, dst in moves:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(src, dst)
            res["cache_linked"] = _link_cache(cfg, live, slug, p["cache"])
            _rewrite_paths(db, s["id"], bdir)
            db.set_bundle(s["id"], bdir, _row_get(s, "uuid"))
            sync_bundle(cfg, db, db.get_series(str(s["rr_id"])))
        else:
            res["cache_linked"] = sum(
                1 for owners in live.values() if slug in owners)
        log(f"  {slug:<18} {res['moved']:>4} files, "
            f"{res['cache_linked']:>6} cache entries")
        out.append(res)
    return out


def reclaim_cache(cfg: Config, db: DB, *, dry_run: bool = False) -> dict:
    """Drop the global-cache link for any segment now living in every bundle
    that references it.

    `migrate` links rather than moves, so a migrated entry has two links and
    the bytes stay pinned until *both* go. That quietly defeats the point:
    `rm -rf <bundle>` would free the audio but not the 6 GB of cache behind it.
    Removing the global link leaves the bundle holding the only reference, so
    deleting a series finally reclaims its space.

    An entry no bundle claims is left alone — that is `cache prune`'s call.
    """
    src_root = os.path.expanduser(cfg.general.cache_dir)
    if not os.path.isdir(src_root):
        return {"unlinked": 0, "kept": 0, "bytes": 0}
    live = _live_keys(cfg, db)
    bdirs = {}
    from .sync import _dir_slug
    for s in db.list_series():
        bdirs[_dir_slug(s)] = bundle_dir(cfg, s)

    unlinked = kept = freed = 0
    for backend in sorted(os.listdir(src_root)):
        sd = os.path.join(src_root, backend)
        if not os.path.isdir(sd):
            continue
        for name in sorted(os.listdir(sd)):
            digest = name.rsplit(".", 1)[0]
            owners = live.get(digest)
            if not owners:
                kept += 1                      # orphan: for `cache prune`
                continue
            # only safe once every owner has its own copy
            if not all(os.path.exists(os.path.join(bdirs.get(o, ""), LAYOUT["cache"],
                                                   backend, name)) for o in owners):
                kept += 1
                continue
            p = os.path.join(sd, name)
            freed += os.path.getsize(p)
            unlinked += 1
            if not dry_run:
                os.unlink(p)
    return {"unlinked": unlinked, "kept": kept, "bytes": freed}


def _live_keys(cfg: Config, db: DB) -> dict:
    """sha1 -> {slugs}, replayed from every bundle's segment scripts. The same
    reconstruction `pipeline._cache_path` does at write time."""
    import hashlib
    from .sync import _dir_slug
    out: dict[str, set] = {}
    for s in db.list_series():
        slug, bdir = _dir_slug(s), bundle_dir(cfg, s)
        for d in (os.path.join(bdir, LAYOUT["chapters"]), bdir):
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
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


def _link_cache(cfg: Config, live: dict, slug: str, dst_root: str) -> int:
    """Hard-link this series' cached segments into its bundle.

    Links rather than moves because a key can be referenced by two series (28
    of 26,813 measured — different books rarely share a sentence). Hard links
    share the inode, so the duplicate costs a directory entry, not 339 KB.
    Unreferenced entries are deliberately left in the global cache for
    `cache prune` to deal with.
    """
    src_root = os.path.expanduser(cfg.general.cache_dir)
    if not os.path.isdir(src_root):
        return 0
    n = 0
    for backend in sorted(os.listdir(src_root)):
        sd = os.path.join(src_root, backend)
        if not os.path.isdir(sd):
            continue
        dd = os.path.join(dst_root, backend)
        for name in os.listdir(sd):
            digest = name.rsplit(".", 1)[0]
            if slug not in live.get(digest, ()):
                continue
            os.makedirs(dd, exist_ok=True)
            target = os.path.join(dd, name)
            if os.path.exists(target):
                n += 1
                continue
            try:
                os.link(os.path.join(sd, name), target)
            except OSError:                     # cross-device: fall back to a copy
                import shutil
                shutil.copy2(os.path.join(sd, name), target)
            n += 1
    return n


def _rewrite_paths(db: DB, sid: int, bdir: str) -> None:
    """Point the DB at the moved files. Absolute, because the state DB is
    machine-local; portability comes from `state.json`, which stores them
    bundle-relative, and `series scan` re-anchors after a move."""
    chapters = os.path.join(bdir, LAYOUT["chapters"])
    for c in db.chapters(sid):
        upd = {}
        for col in _PATH_COLS:
            old = c[col]
            if not old:
                continue
            name = os.path.basename(old)
            new = (os.path.join(bdir, LAYOUT["raw"], name) if col == "raw_path"
                   else os.path.join(chapters, name))
            if os.path.abspath(old) != new:
                upd[col] = new
        if upd:
            db.con.execute(
                f"UPDATE chapters SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?",
                (*upd.values(), c["id"]))
    db.con.commit()


def du(path: str) -> tuple[int, int]:
    """(bytes, files) under `path`, counting each inode once so hard-linked
    cache entries aren't double-counted."""
    total = files = 0
    seen: set = set()
    for root, _, names in os.walk(path):
        for n in names:
            try:
                st = os.stat(os.path.join(root, n), follow_symlinks=False)
            except OSError:
                continue
            files += 1
            if st.st_ino in seen:
                continue
            seen.add(st.st_ino)
            total += st.st_size
    return total, files


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


#: archive suffix -> tarfile mode. `.zst` is handled separately: Python's
#: tarfile has no zstd until 3.14, so it pipes through the `zstd` binary.
_TAR_MODES = {".tar": "w", ".tar.gz": "w:gz", ".tgz": "w:gz",
              ".tar.bz2": "w:bz2", ".tar.xz": "w:xz"}


def _tar_mode(out: str) -> str | None:
    for suffix, mode in _TAR_MODES.items():
        if out.endswith(suffix):
            return mode
    return None


def archive(cfg: Config, db: DB, key: str, *, out: str | None = None,
            with_cache: bool = False, log=print) -> dict:
    """Write one bundle to a tar archive.

    Excludes `.cache/` unless asked: sky-pride is 567 MB of audio against 6 GB
    of regenerable segments, so including it makes an archive 10x larger to
    preserve something a re-render rebuilds.

    No compression by default — the bulk is Opus, which is already compressed,
    so gzip costs minutes and saves almost nothing. Name the output `.tar.zst`
    or `.tar.gz` to override.
    """
    import tarfile
    from .sync import _dir_slug

    s = db.get_series(key)
    if not s:
        raise BundleError("no_such_series", f"no tracked series matching {key!r}",
                          hint="webnovel-audio series list")
    slug, bdir = _dir_slug(s), bundle_dir(cfg, s)
    if not os.path.isdir(bdir):
        raise BundleError("bundle_missing", f"nothing at {bdir}",
                          hint=f"webnovel-audio series forget {slug}")

    # write the machine-owned files first, so the archive carries current state
    sync_bundle(cfg, db, s)

    out = out or f"{slug}.tar"
    skip = () if with_cache else (LAYOUT["cache"],)

    def entries():
        for root, dirs, names in os.walk(bdir):
            rel = os.path.relpath(root, bdir)
            if rel != "." and rel.split(os.sep)[0] in skip:
                dirs[:] = []
                continue
            for n in sorted(names):
                full = os.path.join(root, n)
                yield full, os.path.join(slug, os.path.relpath(full, bdir))

    items = list(entries())
    total = sum(os.path.getsize(f) for f, _ in items if os.path.exists(f))

    if out.endswith(".tar.zst") or out.endswith(".tzst"):
        _write_zst(out, items)
    else:
        mode = _tar_mode(out)
        if mode is None:
            raise BundleError(
                "bad_archive_format",
                f"don't know how to write {os.path.basename(out)}",
                hint="use .tar, .tar.zst, .tar.gz, .tar.bz2 or .tar.xz")
        with tarfile.open(out, mode) as tf:
            for full, arc in items:
                tf.add(full, arcname=arc)

    size = os.path.getsize(out)
    log(f"{s['title']} -> {out}")
    log(f"  {len(items)} files, {human_bytes(total)} -> {human_bytes(size)}"
        + ("" if with_cache else "   (cache excluded)"))
    return {"ok": True, "slug": slug, "out": os.path.abspath(out),
            "files": len(items), "bytes_in": total, "bytes_out": size,
            "with_cache": with_cache}


def _write_zst(out: str, items) -> None:
    """tarfile has no zstd before Python 3.14, so stream through the binary."""
    import shutil
    import subprocess
    import tarfile
    if not shutil.which("zstd"):
        raise BundleError("zstd_missing", "zstd is not installed",
                          hint="write a .tar or .tar.gz instead")
    with open(out, "wb") as fh:
        proc = subprocess.Popen(["zstd", "-q", "-T0", "-"], stdin=subprocess.PIPE,
                                stdout=fh)
        try:
            with tarfile.open(fileobj=proc.stdin, mode="w|") as tf:
                for full, arc in items:
                    tf.add(full, arcname=arc)
        finally:
            proc.stdin.close()
            if proc.wait() != 0:
                raise BundleError("archive_failed", "zstd exited non-zero")


def purge(cfg: Config, db: DB, series_row) -> dict:
    """Delete a series' bundle from disk. One directory — which is the whole
    point of the layout: before it, `forget --purge` could not reach the
    segment cache at all and stranded it permanently."""
    import shutil
    d = bundle_dir(cfg, series_row)
    if not os.path.isdir(d):
        return {"path": d, "removed": False, "bytes": 0, "files": 0}
    size, files = du(d)
    shutil.rmtree(d, ignore_errors=True)
    return {"path": d, "removed": True, "bytes": size, "files": files}


def scan(cfg: Config, db: DB, root: str, *, dry_run: bool = False) -> list[dict]:
    """Import every bundle under `root` (one level deep), and re-locate any
    already-tracked series whose recorded path has moved.

    A tracked series whose directory is *gone* is reported as `missing` and
    left alone: an unplugged drive must never look like a deletion. Removing a
    series stays explicit, via `series forget`.
    """
    root = os.path.abspath(os.path.expanduser(root))
    out: list[dict] = []
    seen: set[str] = set()
    for name in sorted(os.listdir(root) if os.path.isdir(root) else []):
        d = os.path.join(root, name)
        if not os.path.isfile(os.path.join(d, MANIFEST)):
            continue
        try:
            res = import_bundle(cfg, db, d, dry_run=dry_run)
        except BundleError as exc:
            out.append({"ok": False, "path": d, "code": exc.code,
                        "message": exc.message})
            continue
        seen.add(res["uuid"])
        out.append(res)

    for s in db.list_series():
        if _row_get(s, "uuid") in seen:
            continue
        if status(cfg, s) == "missing":
            out.append({"ok": True, "action": "missing", "slug": s["slug"],
                        "title": s["title"], "path": bundle_dir(cfg, s)})
    return out
