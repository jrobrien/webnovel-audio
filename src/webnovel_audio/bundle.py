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

from .config import Config
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
    "narrator", "error",
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
    base_lex = os.path.expanduser(cfg.general.base_lexicon or "")
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
