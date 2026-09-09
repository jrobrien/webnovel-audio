"""Track Royal Road series and batch-render new chapters into a library."""
from __future__ import annotations

import copy
import os
import re
import time
from dataclasses import dataclass

from . import pipeline, providers
from .config import Config
from .db import DB
from .royalroad import _safe_slug, fetch_asset


def _db(cfg: Config) -> DB:
    return DB(cfg.royalroad.state_db)


def _series_provider(source: str) -> providers.Provider:
    prov = providers.resolve_series(source)
    if prov is None:
        raise SystemExit(f"no content provider handles {source!r}")
    return prov


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "series"


def _dir_slug(row) -> str:
    """The on-disk directory name for a series row (defence-in-depth vs. the DB)."""
    return _safe_slug(row["slug"], "") or _slugify(row["title"])


def _series_cfg(cfg: Config, slug: str) -> Config:
    """A per-series view of the config: prefer <lexicon_dir>/<slug>.csv if it exists."""
    path = os.path.join(os.path.expanduser(cfg.general.lexicon_dir or ""), f"{slug}.csv")
    if not os.path.isfile(path) or path == cfg.general.lexicon:
        return cfg
    sc = copy.deepcopy(cfg)
    sc.general.lexicon = path
    return sc


def _cache_cover(cfg: Config, slug: str, cover_url: str) -> None:
    slug = _safe_slug(slug, "")
    if not (cover_url and slug):
        return
    dst = os.path.join(os.path.expanduser(cfg.royalroad.library_dir), slug, "cover.jpg")
    if os.path.exists(dst):
        return
    data = fetch_asset(cover_url)          # cookie-less, Royal Road hosts only
    if not data:
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as fh:
        fh.write(data)


def _chapter_view(c) -> tuple[int, str, bool]:
    """(order, rr_id, unlocked) from a ChapterRef or a sqlite3.Row."""
    if hasattr(c, "order"):
        return c.order, c.rr_id, bool(c.unlocked)
    return c["ord"], c["rr_id"], bool(c["unlocked"])


def _resolve_start(chapters, start: str) -> int:
    """A --from / set value -> the progress_order to store."""
    start = (start or "latest").strip().lower()
    view = [_chapter_view(c) for c in chapters]
    unlocked = [o for o, _, u in view if u]
    if start in ("latest", "current", "caught-up"):
        return max(unlocked) if unlocked else -1
    if start in ("start", "begin", "0", "all"):
        return -1
    if "/chapter/" in start:
        cid = re.search(r"/chapter/(\d+)", start)
        for o, rid, _ in view:
            if cid and rid == cid.group(1):
                return o
        return -1
    if start.isdigit():
        return int(start) - 1        # "you've heard through chapter N"
    return max(unlocked) if unlocked else -1


@dataclass
class SyncResult:
    rendered: int = 0
    errors: int = 0
    skipped: int = 0


def add_series(cfg: Config, url: str, start: str = "latest", log=print) -> dict:
    prov = _series_provider(url)
    db = _db(cfg)
    try:
        fi = prov.series(url, cfg=cfg)
        if not fi.rr_id or not fi.chapters:
            raise SystemExit(f"could not read a fiction + chapter list from {url}")
        sid = db.upsert_series(fi)
        new = db.replace_chapters(sid, fi.chapters)
        db.force_progress(sid, _resolve_start(fi.chapters, start))
        _cache_cover(cfg, fi.slug, fi.cover_url)
        row = db.get_series(fi.rr_id)
        pend = len(db.pending(sid))
        log(f"added: {fi.title}")
        log(f"  {len(fi.chapters)} chapters ({new} new), progress at #{row['progress_order'] + 1}, "
            f"{pend} to render")
        return {"slug": fi.slug, "title": fi.title, "provider": fi.provider,
                "chapters": len(fi.chapters), "new": new,
                "progress": row["progress_order"] + 1, "pending": pend}
    finally:
        db.close()


def refresh(cfg: Config, key: str | None = None, log=print) -> list[dict]:
    db = _db(cfg)
    out: list[dict] = []
    try:
        targets = [db.get_series(key)] if key else db.list_series()
        for s in filter(None, targets):
            fi = _series_provider(s["url"]).series(s["url"], cfg=cfg)
            new = db.replace_chapters(s["id"], fi.chapters)
            _cache_cover(cfg, fi.slug, fi.cover_url)
            log(f"{s['title']}: {len(fi.chapters)} chapters (+{new} new)")
            out.append({"slug": fi.slug, "title": fi.title,
                        "chapters": len(fi.chapters), "new": new})
        return out
    finally:
        db.close()


def make_book(cfg: Config, key: str, *, first: int | None = None, last: int | None = None,
              out: str | None = None, log=print) -> str:
    from .package import build_m4b

    db = _db(cfg)
    try:
        s = db.get_series(key)
        if not s:
            raise SystemExit(f"no tracked series matching {key!r}")
        rows = [c for c in db.chapters(s["id"]) if c["status"] == "rendered"]
        if first is not None:
            rows = [c for c in rows if c["ord"] + 1 >= first]
        if last is not None:
            rows = [c for c in rows if c["ord"] + 1 <= last]
        if not rows:
            raise SystemExit("no rendered chapters in that range")
        lo, hi = rows[0]["ord"] + 1, rows[-1]["ord"] + 1
        span = f"{lo}-{hi}" if lo != hi else str(lo)
        lib = os.path.expanduser(cfg.royalroad.library_dir)
        slug = _dir_slug(s)
        out = out or os.path.join(lib, slug, f"{slug}-ch{span}.m4b")
        cover = os.path.join(lib, slug, "cover.jpg")
        return build_m4b(rows, out, title=f"{s['title']} ({span})", author=s["author"] or "",
                         cover_path=cover if os.path.exists(cover) else None,
                         bitrate=cfg.book.bitrate, log=log)
    finally:
        db.close()


def write_feeds(cfg: Config, key: str | None = None, out_dir: str | None = None,
                base_url: str = "", log=print) -> list[str]:
    from .feed import build_feed

    db = _db(cfg)
    written: list[str] = []
    try:
        lib = os.path.expanduser(cfg.royalroad.library_dir)
        out_dir = out_dir or os.path.join(lib, "_feeds")
        os.makedirs(out_dir, exist_ok=True)
        targets = [db.get_series(key)] if key else db.list_series()
        for s in filter(None, targets):
            base = (base_url or cfg.serve.base_url).rstrip("/")
            dslug = _dir_slug(s)
            cover_local = os.path.exists(os.path.join(lib, dslug, "cover.jpg"))
            xml = build_feed(s, db.chapters(s["id"]), base or "http://localhost:8080",
                             self_url=f"{base}/feed/{dslug}.xml" if base else "",
                             cover_local=cover_local)
            path = os.path.join(out_dir, f"{dslug}.xml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(xml)
            written.append(path)
            log(f"wrote {path}")
        return written
    finally:
        db.close()


def run_sync(cfg: Config, key: str | None = None, *, limit: int | None = None,
             dry_run: bool = False, backend: str = "kokoro", refresh_first: bool = True,
             log=print, emit=None) -> SyncResult:
    """`emit`, if given, is called with structured event dicts (for the UI / --json)."""
    _emit = emit or (lambda _d: None)
    db = _db(cfg)
    res = SyncResult()
    lib = os.path.expanduser(cfg.royalroad.library_dir)
    try:
        targets = list(filter(None, [db.get_series(key)] if key else db.list_series()))
        _emit({"event": "start", "series": len(targets), "dry_run": dry_run})
        for s in targets:
            prov = _series_provider(s["url"])
            if refresh_first:
                fi = prov.series(s["url"], cfg=cfg)
                db.replace_chapters(s["id"], fi.chapters)
            pend = db.pending(s["id"], limit)
            slug = _dir_slug(s)
            _emit({"event": "series", "slug": slug, "title": s["title"], "pending": len(pend)})
            if not pend:
                continue
            log(f"\n{s['title']}: {len(pend)} chapter(s) to render")
            scfg = _series_cfg(cfg, slug)
            out_dir = os.path.join(lib, slug)
            raw_dir = os.path.join(out_dir, ".raw")
            for c in pend:
                num = c["ord"] + 1
                name = f"{num:03d}-{_safe_slug(c['slug'], c['rr_id'])}"
                out_path = os.path.join(out_dir, name + ".opus")
                if dry_run:
                    log(f"  would render #{num} {c['title']}  -> {out_path}")
                    _emit({"event": "chapter", "slug": slug, "number": num,
                           "title": c["title"], "result": "would-render", "path": out_path})
                    res.skipped += 1
                    continue
                _emit({"event": "chapter_begin", "slug": slug, "number": num, "title": c["title"]})
                t0 = time.time()
                try:
                    os.makedirs(raw_dir, exist_ok=True)
                    raw_path = os.path.join(raw_dir, f"{_safe_slug(c['rr_id'], 'chapter')}.html")
                    if not os.path.exists(raw_path):
                        with open(raw_path, "w", encoding="utf-8") as fh:
                            fh.write(prov.raw(c["url"], cfg=cfg))
                    log(f"  #{num} {c['title']}")
                    rep = pipeline.render(raw_path, out_path, scfg, backend=backend,
                                          md_meta={"chapter": num,
                                                   "published": c["published_at"] or ""},
                                          log=lambda *_: None)
                    db.mark(c["id"], "rendered", audio_path=out_path,
                            duration_s=rep.audio_seconds or None)
                    db.set_progress(s["id"], c["ord"])
                    res.rendered += 1
                    _emit({"event": "chapter", "slug": slug, "number": num, "title": c["title"],
                           "result": "rendered", "path": out_path,
                           "audio_seconds": round(rep.audio_seconds, 1),
                           "elapsed_seconds": round(time.time() - t0, 1)})
                except Exception as exc:  # noqa: BLE001 - keep the batch going
                    db.mark(c["id"], "error", error=str(exc)[:400])
                    res.errors += 1
                    log(f"    ! error: {exc}")
                    _emit({"event": "chapter", "slug": slug, "number": num, "title": c["title"],
                           "result": "error", "error": str(exc)[:400]})
        _emit({"event": "done", "rendered": res.rendered, "errors": res.errors,
               "skipped": res.skipped})
        return res
    finally:
        db.close()
