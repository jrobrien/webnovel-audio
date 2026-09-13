"""Track Royal Road series and batch-render new chapters into a library."""
from __future__ import annotations

import contextlib
import copy
import fcntl
import os
import json
import re
import time
import tomllib
from dataclasses import dataclass

from . import bundle, pipeline, providers
from .config import Config
from .db import DB
from .royalroad import _safe_slug, fetch_asset


def _db(cfg: Config) -> DB:
    return DB(cfg.royalroad.state_db)


class SyncLocked(Exception):
    """Another `sync` is already running against this state DB."""

    def __init__(self, path: str):
        super().__init__(f"another sync is already running (lock: {path})")
        self.path = path


def _lock_path(cfg: Config) -> str:
    db_path = os.path.abspath(os.path.expanduser(cfg.royalroad.state_db))
    return os.path.join(os.path.dirname(db_path) or ".", "sync.lock")


@contextlib.contextmanager
def sync_lock(cfg: Config):
    """Exclusive, non-blocking lock so only one `sync` runs per state DB at a time.

    One state DB == one library; two `sync` runs against it race for the same
    CPU and can pile up (UI double-click, a stray terminal invocation, ...) —
    harmless to the data (chapter writes are idempotent) but wasteful to watch.
    `flock` is per-open-file-description and advisory: it's released the instant
    the holding process exits, crash or not, so there's never a stale lock file
    to clean up by hand.
    """
    path = _lock_path(cfg)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SyncLocked(path) from None
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


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


def _series_cfg(cfg: Config, slug: str, bdir: str | None = None) -> Config:
    """A per-series view of the base config: bundle lexicon + TOML overlay, and
    a bundle-local segment cache.

    `bdir` is the series' bundle directory. Without it the pre-bundle locations
    under `data/` are used, so a half-migrated tree still works.
    """
    sc = cfg
    if bdir:
        lex = bundle.resolve(cfg, slug, bdir, "lexicon")
        overlay = bundle.resolve(cfg, slug, bdir, "config")
    else:
        legacy = bundle.legacy_paths(cfg, slug)
        lex, overlay = legacy["lexicon"], legacy["config"]
    if os.path.isfile(lex) and lex != cfg.general.lexicon:
        sc = copy.deepcopy(sc)
        sc.general.lexicon = lex
    if os.path.isfile(overlay):
        sc = sc.overlay(overlay)          # overlay() deep-copies
    if bdir:
        # keep synthesized segments with the series that needs them, so that
        # archiving or deleting a bundle takes its cache with it
        cache = os.path.join(bdir, bundle.LAYOUT["cache"])
        if sc is cfg:
            sc = copy.deepcopy(sc)
        sc.general.cache_dir = cache
    return sc


def _cache_cover(cfg: Config, slug: str, cover_url: str, bdir: str = "") -> None:
    slug = _safe_slug(slug, "")
    if not (cover_url and slug):
        return
    bdir = bdir or os.path.join(os.path.expanduser(cfg.royalroad.library_dir), slug)
    dst = os.path.join(bdir, bundle.LAYOUT["covers"], "cover.jpg")
    if os.path.exists(dst):
        return
    data = fetch_asset(cover_url)          # cookie-less, Royal Road hosts only
    if not data:
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as fh:
        fh.write(data)


def _cache_volume_covers(cfg: Config, slug: str, volumes, bdir: str = "") -> None:
    """Each volume has its own cover on Royal Road — that's half the reason
    volumes are worth modelling. Cached beside the series cover."""
    slug = _safe_slug(slug, "")
    if not slug:
        return
    bdir = bdir or os.path.join(os.path.expanduser(cfg.royalroad.library_dir), slug)
    base = os.path.join(bdir, bundle.LAYOUT["covers"])
    for v in volumes or []:
        if not v.cover_url:
            continue
        dst = os.path.join(base, f"cover-v{_safe_slug(v.rr_id, '0')}.jpg")
        if os.path.exists(dst):
            continue
        data = fetch_asset(v.cover_url)
        if not data:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(data)


def _chapter_view(c) -> tuple[int, str, bool]:
    """(order, rr_id, unlocked) from a ChapterRef or a sqlite3.Row."""
    if hasattr(c, "order"):
        return c.order, c.rr_id, bool(c.unlocked)
    return c["ord"], c["rr_id"], bool(c["unlocked"])


def _skip_through(chapters, start: str) -> int:
    """A `--from` value -> the 1-based chapter number to mark `skipped` up to.

    0 means "skip nothing". This is the only thing `--from` does now: there is
    no separate reader-position marker, just per-chapter status.
    """
    start = (start or "start").strip().lower()
    view = [_chapter_view(c) for c in chapters]
    unlocked = [o for o, _, u in view if u]
    if start in ("start", "begin", "0", "all", ""):
        return 0
    if start in ("latest", "current", "caught-up"):
        return (max(unlocked) + 1) if unlocked else 0
    if "/chapter/" in start:
        cid = re.search(r"/chapter/(\d+)", start)
        for o, rid, _ in view:
            if cid and rid == cid.group(1):
                return o + 1
        return 0
    if start.isdigit():
        return int(start)              # "I've already read through chapter N"
    return 0


@dataclass
class SyncResult:
    rendered: int = 0
    errors: int = 0
    skipped: int = 0


MAX_SEED_CAST_ENTRIES = 15


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _fetch_chapter_blocks(cfg: Config, prov, bdir: str, refs, log=print):
    """refs: iterable of (rr_id, url, ord). Returns (blocks, [ord fetched]).

    Reuses/populates the same `.raw/<rr_id>.html` cache `sync` uses, so a
    chapter sampled here isn't re-fetched when it's actually rendered later.
    One bad chapter is logged and skipped, never raised.
    """
    from .ingest import parse_document

    raw_dir = os.path.join(bdir, bundle.LAYOUT["raw"])
    blocks, fetched = [], []
    for rr_id, url, order in refs:
        try:
            raw_path = os.path.join(raw_dir, f"{_safe_slug(rr_id, 'chapter')}.html")
            if os.path.exists(raw_path):
                html = open(raw_path, encoding="utf-8").read()
            else:
                html = prov.raw(url, cfg=cfg)
                os.makedirs(raw_dir, exist_ok=True)
                with open(raw_path, "w", encoding="utf-8") as fh:
                    fh.write(html)
            blocks += parse_document(html, url=url).blocks
            fetched.append(order)
        except Exception as exc:  # noqa: BLE001 - one bad chapter shouldn't sink the rest
            log(f"  couldn't read chapter #{order + 1}: {exc}")
    return blocks, fetched


def _cast_voices_lines(named, voices, gender, counts, default_voice, provenance: str):
    """The rows to splice into `[cast.voices]`, with provenance as a comment
    *inside* the table (not a file header) so repeated `cast update` runs leave a
    readable history of where each speaker came from."""
    shown, extra = named[:MAX_SEED_CAST_ENTRIES], named[MAX_SEED_CAST_ENTRIES:]
    lines = [f"# {provenance}"]
    for name, count in shown:
        g = {"m": "male", "f": "female"}.get(gender.get(name), "")
        note = f"{count} line(s)" + (f", {g}" if g else ", gender unclear — pick one")
        lines.append(f'{_toml_str(name):<18} = "{voices[name]}"   # {note}')
    if extra:
        lines.append(f"#   + {len(extra)} rarer speaker(s) omitted -> [cast] default")
    unattributed = counts.get("", 0)
    if unattributed:
        lines.append(f"#   {unattributed} line(s) unattributed -> "
                     f"[cast] default ({default_voice})")
    return shown, lines


def set_cast_voice(cfg: Config, slug: str, speaker: str, voice: str,
                   bdir: str = "") -> dict:
    """Assign one speaker a voice in the series overlay, non-interactively.

    The agent-facing counterpart to `cast edit`: rewrites the speaker's row in
    place if it exists (keeping its trailing comment), appends it otherwise, and
    leaves the rest of the file byte-identical. An empty `voice` means
    "listed but unassigned" -> falls through to [cast] default.
    """
    path = (bundle.resolve(cfg, slug, bdir, "config") if bdir
            else bundle.legacy_paths(cfg, slug)["config"])
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else \
        pin_defaults_text(cfg, slug)

    quoted = _toml_str(speaker)
    line = f'{quoted:<18} = "{voice}"'
    out, replaced, insec = [], False, False
    for ln in text.splitlines():
        stripped = ln.strip()
        if stripped.startswith("["):
            insec = stripped == "[cast.voices]"
        elif insec and not replaced:
            key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
            if key in (quoted, speaker):
                comment = ln.split("#", 1)[1] if "#" in ln else ""
                out.append(line + (f"   #{comment}" if comment else ""))
                replaced = True
                continue
        out.append(ln)
    if not replaced:
        out = _append_cast_voices("\n".join(out) + "\n", [line]).splitlines()

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out).rstrip("\n") + "\n")
    return {"path": path, "speaker": speaker, "voice": voice,
            "action": "updated" if replaced else "added"}


def _existing_cast_voice_keys(text: str) -> set[str]:
    """Best-effort: speaker names already given a voice under [cast.voices]."""
    try:
        data = tomllib.loads(text)
    except Exception:  # noqa: BLE001 - a hand-edited file may not parse; treat as empty
        return set()
    return set((data.get("cast") or {}).get("voices") or {})


def _append_cast_voices(text: str, new_lines: list[str]) -> str:
    """Insert `new_lines` into an existing [cast.voices] table, or add one at the
    end if there isn't one — everything else in `text` is left untouched."""
    lines = text.splitlines()
    hdr = next((i for i, ln in enumerate(lines) if ln.strip() == "[cast.voices]"), None)
    if hdr is None:
        if lines and lines[-1].strip():
            lines.append("")
        return "\n".join([*lines, "[cast.voices]", *new_lines]) + "\n"
    end = len(lines)
    for j in range(hdr + 1, len(lines)):
        if lines[j].lstrip().startswith("[") and lines[j].rstrip().endswith("]"):
            end = j
            break
    while end > hdr + 1 and not lines[end - 1].strip():
        end -= 1                              # insert before trailing blank lines
    return "\n".join([*lines[:end], *new_lines, *lines[end:]]) + "\n"


def pin_defaults_text(cfg: Config, slug: str) -> str:
    """The starting content for a series config: the *resolved* voices, written
    out explicitly.

    Pinning them at init is what keeps a series sounding the same. Global
    `[voices]`/`[cast]` defaults get retuned as you start new series; without a
    pin, re-rendering chapter 12 of an old one a year later would come out in a
    different voice than 11 and 13. Same reasoning as a lockfile: record what was
    resolved, so a later change to the source doesn't silently alter the result.
    Timing/DSP are deliberately not pinned — they're global taste, and a change
    there is one you want everywhere.
    """
    v, cast = cfg.voices, cfg.cast
    lines = [
        f"# {slug} — voices pinned at `series add` so this series keeps sounding",
        "# the same even if the global defaults in config.toml change later.",
        "",
        "[voices]",
        f'narrator         = "{v.narrator}"',
        f'thought          = "{v.thought}"',
        f'dialogue_default = "{v.dialogue_default}"',
        f'system_ui        = "{v.system_ui}"',
        "",
        "[cast]",
        f'default = "{cast.default or v.dialogue_default}"',
    ]
    if cast.narrator:
        lines.append(f'narrator = "{cast.narrator}"')
    if cfg.chat.voices:
        pool = ", ".join(f'"{x}"' for x in cfg.chat.voices)
        lines += ["", "[chat]", f"voices = [{pool}]"]
    lines += ["", "[cast.voices]",
              "# speaker -> voice. `cast update <slug> <range>` appends new ones;",
              '# an empty "" means "found, not yet assigned" -> [cast] default.', ""]
    return "\n".join(lines) + "\n"


def suggest_cast(cfg: Config, key: str, *, spans=None,
                 lo: int | None = None, hi: int | None = None,
                 write_lexicon: bool = False, apply_cast: bool = False,
                 context: int = 6, log=print) -> dict:
    """The `check` stage: sample a chapter range of a tracked series and
    an arbitrary chapter range — e.g. re-run on #50-55 once new characters show
    up, without disturbing voices you already assigned for earlier chapters.

    Also flags known heteronyms (`heteronyms.find_heteronyms`) and proper nouns
    missing from any lexicon, over the same sampled text — one pass covers all
    three questions ("who needs a voice", "what might be mispronounced by
    context", "what needs a respelling") at once. Returns a report dict;
    `write_lexicon=True` also appends the unknown names to the per-series CSV.
    """
    from . import dialogue
    from .heteronyms import find_heteronyms
    from .lexicon import Lexicon
    from .segment import find_unknown_names

    db = _db(cfg)
    try:
        s = db.get_series(key)
        if not s:
            raise SystemExit(f"no tracked series matching {key!r}")
        slug = _dir_slug(s)
        if spans is None:
            spans = [(lo, hi)] if (lo is not None or hi is not None) else []
        if not spans:
            spans = [(None, cfg.cast.seed_chapters)]
        sample = [c for c in db.select(s["id"], spans) if c["unlocked"]]
        empty = {"slug": slug, "title": s["title"], "chapters_sampled": [], "cast": {},
                 "heteronyms": [], "lexicon_candidates": [], "overlay_path": None,
                 "overlay_action": "none"}
        if not sample:
            log(f"{s['title']}: no chapters in that range")
            return empty

        prov = _series_provider(s["url"])
        refs = [(c["rr_id"], c["url"], c["ord"]) for c in sample]
        bdir = bundle.bundle_dir(cfg, s)
        blocks, fetched = _fetch_chapter_blocks(cfg, prov, bdir, refs, log=log)
        if not fetched:
            log(f"{s['title']}: no chapters could be sampled")
            return empty

        scfg = _series_cfg(cfg, slug, bdir)    # respects voices/lexicon already assigned
        counts, gender = dialogue.discover(blocks, scfg)
        named = sorted(((k, v) for k, v in counts.items() if k), key=lambda kv: -kv[1])
        voices = dialogue.suggest_voices(dict(named), gender, scfg) if named else {}

        text = "\n".join(b.text for b in blocks if b.kind == "paragraph")
        heteronyms = find_heteronyms(text, context=context)

        series_lex = bundle.resolve(cfg, slug, bdir, "lexicon")
        known: set[str] = set()
        base_lex = scfg.general.base_lexicon
        if base_lex and os.path.exists(base_lex):
            known |= Lexicon.load(base_lex).surfaces()
        if os.path.exists(series_lex):
            known |= Lexicon.load(series_lex).surfaces()
        candidates = [n for n, _ in find_unknown_names(blocks, known)]

        overlay_path = bundle.resolve(cfg, slug, bdir, "config")
        existing_text = open(overlay_path, encoding="utf-8").read() \
            if os.path.exists(overlay_path) else ""
        existing_keys = _existing_cast_voice_keys(existing_text) if existing_text else set()
        new_named = [(n, c) for n, c in named if n not in existing_keys]

        overlay_action = "unchanged"
        if new_named and not apply_cast:
            overlay_action = "would-add"
        elif new_named:
            default_voice = scfg.cast.default or scfg.voices.dialogue_default
            lo_n, hi_n = sample[0]["ord"] + 1, sample[-1]["ord"] + 1
            _, new_lines = _cast_voices_lines(
                new_named, voices, gender, counts, default_voice,
                f"from chapters {lo_n}-{hi_n} ({len(fetched)} sampled)")
            if not existing_text:
                os.makedirs(os.path.dirname(overlay_path), exist_ok=True)
                existing_text = pin_defaults_text(scfg, slug)
                overlay_action = "created"
            else:
                overlay_action = "appended"
            with open(overlay_path, "w", encoding="utf-8") as fh:
                fh.write(_append_cast_voices(existing_text, new_lines))

        log(f"{s['title']}: sampled chapters {sample[0]['ord'] + 1}-{sample[-1]['ord'] + 1} "
            f"({len(fetched)} chapter(s), {len(sample) - len(fetched)} failed)")
        return {
            "slug": slug, "title": s["title"],
            "chapters_sampled": [o + 1 for o in fetched],
            "cast": {n: {"voice": voices[n], "count": c, "gender": gender.get(n) or "?",
                        "new": n not in existing_keys} for n, c in named},
            "heteronyms": [{"word": w, "count": c, "context": ctx} for w, c, ctx in heteronyms],
            "lexicon_candidates": candidates,
            "overlay_path": overlay_path if new_named else (overlay_path if existing_text else None),
            "overlay_action": overlay_action,
        }
    finally:
        db.close()


def add_series(cfg: Config, url: str, start: str = "latest", log=print) -> dict:
    """Register a series: metadata + chapter list only. No chapter downloads —
    `fetch`/`parse`/`check`/`render` are separate, explicit stages."""
    prov = _series_provider(url)
    db = _db(cfg)
    try:
        fi = prov.series(url, cfg=cfg)
        if not fi.rr_id or not fi.chapters:
            raise SystemExit(f"could not read a fiction + chapter list from {url}")
        sid = db.upsert_series(fi)
        db.replace_volumes(sid, fi.volumes)
        new = db.replace_chapters(sid, fi.chapters)
        through = _skip_through(fi.chapters, start)
        if through:
            db.set_status([c["id"] for c in db.range(sid, None, through)
                           if c["status"] == "new"], "skipped")
        row = db.get_series(fi.rr_id)
        bdir = bundle.bundle_dir(cfg, row)
        _cache_cover(cfg, fi.slug, fi.cover_url, bdir)
        _cache_volume_covers(cfg, fi.slug, fi.volumes, bdir)
        pend = len(db.pending(sid))

        # pin the resolved voices now, so this series keeps sounding the same
        # when the global defaults are retuned later (see pin_defaults_text)
        slug = _dir_slug(row)
        overlay = os.path.join(bdir, bundle.LAYOUT["config"])
        if not os.path.exists(overlay):
            os.makedirs(os.path.dirname(overlay), exist_ok=True)
            with open(overlay, "w", encoding="utf-8") as fh:
                fh.write(pin_defaults_text(cfg, slug))
            log(f"  voices pinned -> {overlay}")
        bundle.sync_bundle(cfg, db, row)
        log(f"added: {fi.title}")
        log(f"  {len(fi.chapters)} chapters ({new} new), "
            f"{through} skipped, {pend} outstanding")
        log(f"  next:  webnovel-audio fetch {slug} 1-10")
        return {"slug": fi.slug, "title": fi.title, "provider": fi.provider,
                "chapters": len(fi.chapters), "new": new,
                "skipped": through, "pending": pend}
    finally:
        db.close()


def refresh(cfg: Config, key: str | None = None, log=print) -> list[dict]:
    db = _db(cfg)
    out: list[dict] = []
    try:
        targets = [db.get_series(key)] if key else db.list_series()
        for s in filter(None, targets):
            fi = _series_provider(s["url"]).series(s["url"], cfg=cfg)
            # refresh the series metadata too, not just the chapter list: tags,
            # warnings, status and rating all drift, and a series added before
            # those columns existed would otherwise stay null forever
            db.upsert_series(fi)
            db.replace_volumes(s["id"], fi.volumes)
            new = db.replace_chapters(s["id"], fi.chapters)
            bdir = bundle.bundle_dir(cfg, s)
            _cache_cover(cfg, fi.slug, fi.cover_url, bdir)
            _cache_volume_covers(cfg, fi.slug, fi.volumes, bdir)
            log(f"{s['title']}: {len(fi.chapters)} chapters (+{new} new)")
            bundle.sync_bundle(cfg, db, db.get_series(str(s["rr_id"])))
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
        bdir = bundle.bundle_dir(cfg, s)
        slug = _dir_slug(s)
        out = out or os.path.join(bdir, f"{slug}-ch{span}.m4b")
        cover = bundle.resolve(cfg, slug, bdir, "covers")
        cover = os.path.join(cover, "cover.jpg") if os.path.isdir(cover) else cover
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
            bdir = bundle.bundle_dir(cfg, s)
            cover_local = os.path.exists(
                os.path.join(bdir, bundle.LAYOUT["covers"], "cover.jpg")) or \
                os.path.exists(os.path.join(bdir, "cover.jpg"))
            xml = build_feed(s, db.chapters(s["id"]), base or "http://localhost:8080",
                             volumes=db.volume_map(s["id"]),
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




# --- the pipeline ----------------------------------------------------------
#
# One engine drives every stage. A chapter walks new -> fetched -> parsed ->
# rendered; `_advance` runs whatever is missing to reach `upto`, so a stage
# always pulls its own inputs (`render 20-30` on unfetched chapters fetches
# them). `force` redoes only the *named* stage — the earlier ones stay cached,
# so re-rendering never re-downloads.

def _raw_path(bdir: str, prov, c) -> str:
    ext = getattr(prov, "raw_ext", ".html")
    return os.path.join(bdir, bundle.LAYOUT["raw"],
                        f"{_safe_slug(c['rr_id'], 'chapter')}{ext}")


def _out_stem(bdir: str, c) -> str:
    name = f"{c['ord'] + 1:03d}-{_safe_slug(c['slug'], c['rr_id'])}"
    return os.path.join(bdir, bundle.LAYOUT["chapters"], name)


class ChapterLocked(Exception):
    """The source says this chapter needs an account we don't have."""


def _do_fetch(cfg, db, prov, bdir, c, *, force=False) -> str:
    path = _raw_path(bdir, prov, c)
    if force or not os.path.exists(path):
        # The session cookie is never verified up front (see royalroad.py) — a
        # missing or stale one surfaces here, where it's actionable.
        if not c["unlocked"]:
            from .royalroad import load_session
            hint = ("run `webnovel-audio login` first" if not load_session()
                    else "stored session may be stale — re-run `webnovel-audio login`")
            raise ChapterLocked(f"chapter is marked locked; {hint}")
        text = prov.raw(c["url"], cfg=cfg)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    db.mark_stage(c["id"], "fetched", raw_path=path)
    return path


def _do_parse(cfg, db, prov, bdir, c, raw_path, *, force=False) -> str:
    from .pipeline import load_document
    from .textout import render_markdown

    md_path = _out_stem(bdir, c) + ".md"
    if force or not os.path.exists(md_path):
        _, _, doc = load_document(raw_path, cfg)
        if doc is not None:
            os.makedirs(os.path.dirname(md_path), exist_ok=True)
            with open(md_path, "w", encoding="utf-8") as fh:
                fh.write(render_markdown(doc, front_matter_extra={
                    "chapter": c["ord"] + 1, "published": c["published_at"] or ""}))
    db.mark_stage(c["id"], "parsed",
                  text_path=md_path if os.path.exists(md_path) else None)
    return md_path


def _opus_tags(scfg, series_row, c, vol_info=None, rendered_at=None) -> dict:
    """Series/chapter provenance for the Opus stream (Vorbis comments).

    Verified to round-trip through libopus, custom keys included. No synopsis —
    that's the author's text; the source URL stands in for it.
    """
    keys = series_row.keys()
    warnings = []
    if "warnings" in keys and series_row["warnings"]:
        try:
            warnings = json.loads(series_row["warnings"]) or []
        except (ValueError, TypeError):
            warnings = []
    tags = []
    if "tags" in keys and series_row["tags"]:
        try:
            tags = json.loads(series_row["tags"]) or []
        except (ValueError, TypeError):
            tags = []
    vol = vol_info or {}
    out = {
        # volume-relative track when the chapter is in a volume, else global
        "TRACKNUMBER": str(c["volume_chapter"] or c["ord"] + 1),
        "DATE": (c["published_at"] or "")[:10],      # the chapter's publish date
        "PERFORMER": scfg.cast.narrator or scfg.voices.narrator,
        "ORGANIZATION": "webnovel-audio (Kokoro-82M)",
        # when the audio was actually made — never "now", or a retag would
        # silently rewrite real provenance
        "RENDERED_AT": rendered_at or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "CONTENT_WARNING": "; ".join(warnings),
        "KEYWORDS": "; ".join(tags[:20]),
        "VOLUME": vol.get("title", ""),
        "VOLUME_INDEX": str(vol["index"]) if vol.get("index") else "",
    }
    if vol.get("title"):
        out["album"] = f'{series_row["title"]} — {vol["title"]}'
    return {k: v for k, v in out.items() if v}


def _do_render(cfg, db, scfg, bdir, c, raw_path, *, backend="kokoro",
               series_row=None, vol_info=None) -> tuple[str, float]:
    out_path = _out_stem(bdir, c) + ".opus"
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    rep = pipeline.render(raw_path, out_path, scfg, backend=backend,
                          md_meta={"chapter": c["ord"] + 1,
                                   "published": c["published_at"] or ""},
                          tags=(_opus_tags(scfg, series_row, c, vol_info,
                                           rendered_at=started)
                                if series_row is not None else None),
                          log=lambda *_: None)
    db.mark_stage(c["id"], "rendered", audio_path=out_path,
                  duration_s=rep.audio_seconds or None,
                  narrator=scfg.cast.narrator or scfg.voices.narrator,
                  render_started_at=started,
                  render_ended_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    return out_path, rep.audio_seconds, rep


def _advance(cfg, db, prov, scfg, bdir, c, *, upto, force=False, backend="kokoro",
             series_row=None, vol_map=None):
    """Walk one chapter up to `upto`. Returns an event dict for the caller."""
    from .db import stage_rank

    have = stage_rank(c["status"])
    want = stage_rank(upto)
    ev = {"number": c["ord"] + 1, "title": c["title"], "stage": upto}

    raw = c["raw_path"] if c["raw_path"] and os.path.exists(c["raw_path"]) else None
    if want >= stage_rank("fetched"):
        if raw is None or (force and upto == "fetched"):
            raw = _do_fetch(cfg, db, prov, bdir, c, force=force and upto == "fetched")
            ev["fetched"] = True
        elif have < stage_rank("fetched"):
            db.mark_stage(c["id"], "fetched", raw_path=raw)
    if want >= stage_rank("parsed"):
        _do_parse(cfg, db, prov, bdir, c, raw, force=force and upto == "parsed")
        ev["parsed"] = True
    if want >= stage_rank("rendered"):
        vol = (vol_map or {}).get(c["volume_rr_id"]) if c["volume_rr_id"] else None
        path, secs, rep = _do_render(cfg, db, scfg, bdir, c, raw, backend=backend,
                                     series_row=series_row, vol_info=vol)
        ev["path"] = path
        ev["audio_seconds"] = round(secs, 1)
        # a fully cached re-render finishes in seconds; say so, or it looks wrong
        ev["cached_segments"] = rep.cached_segments
        ev["total_segments"] = rep.total_segments
    ev["result"] = "ok"
    return ev


def run_stage(cfg: Config, stage: str, key: str | None = None, *,
              spans=None, lo: int | None = None, hi: int | None = None,
              limit: int | None = None,
              backend: str | None = None, dry_run: bool = False,
              refresh_first: bool = False, log=print, emit=None) -> SyncResult:
    """Run one pipeline stage over a series (or every enabled series).

    An explicit range is imperative: those exact chapters, whatever their state.
    No range is declarative: whatever hasn't reached `stage` yet.
    """
    _emit = emit or (lambda _d: None)
    if spans is None:
        spans = [(lo, hi)] if (lo is not None or hi is not None) else []
    explicit = bool(spans)
    backend = backend or cfg.synth.backend
    db = _db(cfg)
    res = SyncResult()
    try:
        if key:
            s = db.get_series(key)
            if not s:
                raise SystemExit(f"no tracked series matching {key!r}")
            targets = [s]
        else:
            targets = [s for s in db.list_series() if s["enabled"]]
        _emit({"event": "start", "stage": stage, "series": len(targets),
               "dry_run": dry_run, "range": spans or None})

        touched: list[str] = []
        for s in targets:
            slug = _dir_slug(s)
            prov = _series_provider(s["url"])
            bdir = bundle.bundle_dir(cfg, s)
            if refresh_first:
                fi = prov.series(s["url"], cfg=cfg)
                db.upsert_series(fi)
                db.replace_volumes(s["id"], fi.volumes)
                db.replace_chapters(s["id"], fi.chapters)
                # single-artifact providers (a Gutenberg .txt) pull once, here
                prov.prefetch(fi, cfg=cfg,
                              cache_dir=os.path.join(bdir, bundle.LAYOUT["raw"]))
            todo = (db.select(s["id"], spans) if explicit
                    else db.outstanding(s["id"], stage, limit))
            if explicit and limit:
                todo = todo[:limit]
            _emit({"event": "series", "slug": slug, "title": s["title"],
                   "pending": len(todo)})
            if not todo:
                continue
            touched.append(str(s["rr_id"]))
            log(f"\n{s['title']}: {len(todo)} chapter(s) to {stage.rstrip('ed')}"
                f"{' (explicit range)' if explicit else ''}")
            scfg = _series_cfg(cfg, slug, bdir)
            vol_map = db.volume_map(s["id"])
            for c in todo:
                num = c["ord"] + 1
                if dry_run:
                    log(f"  would {stage.rstrip('ed')} #{num} {c['title']}")
                    _emit({"event": "chapter", "slug": slug, "number": num,
                           "title": c["title"], "result": "would-run", "stage": stage})
                    res.skipped += 1
                    continue
                _emit({"event": "chapter_begin", "slug": slug, "number": num,
                       "title": c["title"], "stage": stage})
                t0 = time.time()
                try:
                    log(f"  #{num} {c['title']}")
                    ev = _advance(cfg, db, prov, scfg, bdir, c, upto=stage,
                                  force=explicit, backend=backend, series_row=s,
                                  vol_map=vol_map)
                    res.rendered += 1
                    hit, tot = ev.get("cached_segments"), ev.get("total_segments")
                    if hit:
                        # a fully cached re-render finishes in seconds; without
                        # this it just looks like the render silently skipped
                        log(f"      {hit}/{tot} segments from cache")
                    _emit({"event": "chapter", "slug": slug,
                           "elapsed_seconds": round(time.time() - t0, 1), **ev})
                except Exception as exc:  # noqa: BLE001 - keep the batch going
                    db.mark(c["id"], "error", error=str(exc)[:400], error_stage=stage)
                    res.errors += 1
                    log(f"    ! error: {exc}")
                    _emit({"event": "chapter", "slug": slug, "number": num,
                           "title": c["title"], "result": "error", "stage": stage,
                           "error": str(exc)[:400]})
        if not dry_run:
            for row in filter(None, (db.get_series(k) for k in touched)):
                bundle.sync_bundle(cfg, db, row)
        _emit({"event": "done", "stage": stage, "done": res.rendered,
               "errors": res.errors, "skipped": res.skipped})
        return res
    finally:
        db.close()


def run_sync(cfg: Config, key: str | None = None, *, limit: int | None = None,
             dry_run: bool = False, backend: str = "kokoro", refresh_first: bool = True,
             log=print, emit=None) -> SyncResult:
    """The daily driver: refresh chapter lists, then render everything outstanding."""
    return run_stage(cfg, "rendered", key, limit=limit, backend=backend,
                     dry_run=dry_run, refresh_first=refresh_first, log=log, emit=emit)


# Seconds of wall time per second of rendered audio — only the fallback for a
# series with nothing timed yet. Once `chapters.render_started_at`/`_ended_at`
# has >= MIN_COST_SAMPLES rows the ratio is measured from those instead, so the
# estimate tracks this machine, this config and this series.
#
# The first seed here was 0.40, from a single 223 s chapter rendered cold. That
# turned out ~2x pessimistic: Kokoro's model load is a fixed cost that a short
# chapter can't amortise. Measured over 13 real chapters (2.4 h of audio) the
# steady-state ratio is 0.200, range 0.193-0.233. Kept slightly above the
# measurement so a fresh series over-estimates rather than under-estimates —
# being told a job is shorter than it is, is the worse failure.
RENDER_COST_RATIO = 0.25
MIN_COST_SAMPLES = 3            # below this, the median is too noisy to trust
_FALLBACK_CHAPTER_SECONDS = 900.0        # ~15 min, if we've never rendered any


def estimate_render(cfg: Config, key: str | None = None, *,
                    limit: int | None = None) -> dict:
    """How much work an implicit-range render/sync is about to do.

    Duration is predicted from the median of what this series has already
    rendered — series differ a lot in chapter length — falling back to a global
    guess for a series with nothing rendered yet.
    """
    import statistics

    db = _db(cfg)
    try:
        rows = ([db.get_series(key)] if key
                else [s for s in db.list_series() if s["enabled"]])
        per_series, total_ch, total_s = [], 0, 0.0
        for s in filter(None, rows):
            todo = db.outstanding(s["id"], "rendered", limit)
            if not todo:
                continue
            known = [c["duration_s"] for c in db.chapters(s["id"])
                     if c["status"] == "rendered" and c["duration_s"]]
            per_ch = statistics.median(known) if known else _FALLBACK_CHAPTER_SECONDS
            timed = db.render_samples(s["id"])
            if len(timed) >= MIN_COST_SAMPLES:
                ratio = statistics.median(w / a for a, w in timed if a > 0)
            else:
                ratio = RENDER_COST_RATIO
            secs = len(todo) * per_ch * ratio
            per_series.append({"slug": _dir_slug(s), "title": s["title"],
                               "chapters": len(todo), "seconds": secs,
                               "from_samples": len(known),
                               "cost_ratio": round(ratio, 3),
                               "timed_samples": len(timed)})
            total_ch += len(todo)
            total_s += secs
        return {"chapters": total_ch, "seconds": total_s, "series": per_series}
    finally:
        db.close()


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
