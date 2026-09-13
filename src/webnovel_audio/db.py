"""SQLite library state: tracked series and the per-chapter pipeline stage.

A chapter walks one ordered path — new -> fetched -> parsed -> rendered — plus
two off-path states: `error` (with `error_stage` naming which stage broke, so a
render failure doesn't lose the fact that it *was* fetched and parsed) and
`skipped` (deliberately not wanted: behind `series add --from N`, or set by
hand). `status` is the *only* notion of progress in the system — there is no
separate "how far the reader has got" marker, and nothing here talks to Royal
Road about reading position.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid

# the ordered pipeline; index = how far a chapter has progressed
STAGES = ("new", "fetched", "parsed", "rendered")
STATUSES = (*STAGES, "error", "skipped")


def stage_rank(status: str) -> int:
    """How far along `status` is. `error`/`skipped` sort before any real stage."""
    try:
        return STAGES.index(status)
    except ValueError:
        return -1


_SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id             INTEGER PRIMARY KEY,
    rr_id          TEXT UNIQUE NOT NULL,   -- provider-native id
    provider       TEXT DEFAULT 'royalroad',
    slug           TEXT,
    title          TEXT,
    author         TEXT,
    url            TEXT,
    cover_url      TEXT,
    enabled        INTEGER DEFAULT 1,    -- 0 = excluded from `sync`
    tags           TEXT,                 -- JSON array, from the source page
    warnings       TEXT,                 -- JSON array: "Graphic Violence", ...
    status         TEXT,                 -- ONGOING | COMPLETED | ...
    rating         REAL,
    added_at       TEXT,
    uuid           TEXT,                 -- stable identity; survives moves and re-import
    path           TEXT                  -- absolute bundle directory (machine-local)
);
CREATE TABLE IF NOT EXISTS chapters (
    id           INTEGER PRIMARY KEY,
    series_id    INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    rr_id        TEXT NOT NULL,
    ord          INTEGER NOT NULL,
    title        TEXT,
    slug         TEXT,
    url          TEXT,
    published_at TEXT,
    unlocked     INTEGER DEFAULT 1,
    status       TEXT DEFAULT 'new',  -- new|fetched|parsed|rendered|error|skipped
    error_stage  TEXT,                -- which stage failed, when status='error'
    raw_path     TEXT,
    text_path    TEXT,
    audio_path   TEXT,
    duration_s   REAL,
    fetched_at   TEXT,
    parsed_at    TEXT,
    rendered_at  TEXT,
    render_started_at TEXT,   -- wall clock around the synth, for cost estimates
    render_ended_at   TEXT,
    volume_rr_id      TEXT,   -- provider volumeId; NULL is normal (see replace_volumes)
    volume_chapter    INTEGER,-- 1-based position within its volume
    narrator          TEXT,   -- voice actually used, recorded at render time
    synth_fingerprint TEXT,   -- which synth generation produced this audio
    error        TEXT,
    UNIQUE (series_id, rr_id)
);
CREATE TABLE IF NOT EXISTS volumes (
    id          INTEGER PRIMARY KEY,
    series_id   INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    rr_id       TEXT NOT NULL,        -- provider volumeId
    title       TEXT,
    cover_url   TEXT,
    ord         INTEGER,              -- provider order; NOT contiguous (RR skips)
    UNIQUE (series_id, rr_id)
);
CREATE INDEX IF NOT EXISTS chapters_series_ord ON chapters(series_id, ord);
"""


class DB:
    def __init__(self, path: str):
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.con = sqlite3.connect(path, timeout=10.0)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self.con.execute("PRAGMA journal_mode = WAL")   # UI can read while sync writes
        self.con.execute("PRAGMA busy_timeout = 10000")
        self.con.executescript(_SCHEMA)
        self._migrate()
        self.con.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(chapters)")}
        for name, decl in (("duration_s", "REAL"), ("error_stage", "TEXT"),
                           ("raw_path", "TEXT"), ("text_path", "TEXT"),
                           ("fetched_at", "TEXT"), ("parsed_at", "TEXT"),
                           ("render_started_at", "TEXT"), ("render_ended_at", "TEXT"),
                           ("volume_rr_id", "TEXT"), ("volume_chapter", "INTEGER"),
                           ("narrator", "TEXT"), ("synth_fingerprint", "TEXT")):
            if name not in cols:
                self.con.execute(f"ALTER TABLE chapters ADD COLUMN {name} {decl}")
        scols = {r["name"] for r in self.con.execute("PRAGMA table_info(series)")}
        if "provider" not in scols:
            self.con.execute("ALTER TABLE series ADD COLUMN provider TEXT DEFAULT 'royalroad'")
        if "enabled" not in scols:
            self.con.execute("ALTER TABLE series ADD COLUMN enabled INTEGER DEFAULT 1")
        for name, decl in (("tags", "TEXT"), ("warnings", "TEXT"),
                           ("status", "TEXT"), ("rating", "REAL"),
                           ("uuid", "TEXT"), ("path", "TEXT")):
            if name not in scols:
                self.con.execute(f"ALTER TABLE series ADD COLUMN {name} {decl}")

        if "error_stage" not in cols:
            # 0.1 -> 0.2. Old `status` only knew new|rendered|error|skipped, and
            # `pending()` also required `ord > progress_order`, so a 'new' chapter
            # behind the marker was invisible to every command. Backfill the finer
            # stages from what's on disk and make that intent explicit instead.
            self.con.execute(
                "UPDATE chapters SET error_stage='render' WHERE status='error'")
            self.con.execute(
                """UPDATE chapters SET rendered_at=COALESCE(rendered_at, ?)
                   WHERE status='rendered' AND rendered_at IS NULL""",
                (time.strftime("%Y-%m-%dT%H:%M:%S"),))
            # chapters the reader had passed but never rendered were skipped by
            # intent (`--from N`); say so, rather than leaving them unreachable.
            self.con.execute(
                """UPDATE chapters SET status='skipped'
                   WHERE status='new' AND ord <= (
                       SELECT progress_order FROM series WHERE id=chapters.series_id)""")

        # Every series needs a stable identity before it can be exported to a
        # bundle and re-imported elsewhere; the autoincrement `id` is local to
        # this file and means nothing on another machine.
        for r in self.con.execute(
                "SELECT id FROM series WHERE uuid IS NULL OR uuid=''").fetchall():
            self.con.execute("UPDATE series SET uuid=? WHERE id=?",
                             (str(uuid.uuid4()), r["id"]))

        if "progress_order" in scols:
            # 0.2.1: the separate reader-position marker is gone. Its only real
            # job — "I've already read 1..N" — is now per-chapter `skipped`,
            # folded in just above.
            self.con.execute("ALTER TABLE series DROP COLUMN progress_order")

    # -- series ------------------------------------------------------------
    def upsert_series(self, fi) -> int:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        tags = json.dumps(getattr(fi, "tags", []) or [])
        warnings = json.dumps(getattr(fi, "warnings", []) or [])
        cur = self.con.execute(
            # `uuid` is assigned on insert and never updated — it is this
            # series' identity across exports, moves and re-imports.
            """INSERT INTO series (rr_id, provider, slug, title, author, url, cover_url,
                                   tags, warnings, status, rating, added_at, uuid)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(rr_id) DO UPDATE SET
                 provider=excluded.provider, slug=excluded.slug, title=excluded.title,
                 author=excluded.author, url=excluded.url, cover_url=excluded.cover_url,
                 tags=excluded.tags, warnings=excluded.warnings,
                 status=excluded.status, rating=excluded.rating""",
            (fi.rr_id, getattr(fi, "provider", "royalroad"), fi.slug, fi.title,
             fi.author, fi.url, fi.cover_url, tags, warnings,
             getattr(fi, "status", ""), getattr(fi, "rating", 0.0), now,
             str(uuid.uuid4())),
        )
        self.con.commit()
        row = self.get_series(fi.rr_id)
        return row["id"] if row else cur.lastrowid

    def get_series(self, key: str):
        key = str(key)
        row = self.con.execute(
            "SELECT * FROM series WHERE rr_id=? OR slug=?", (key, key)
        ).fetchone()
        if row:
            return row
        return self.con.execute(
            """SELECT * FROM series
               WHERE slug LIKE ? OR slug LIKE ? OR title LIKE ?
               ORDER BY id LIMIT 1""",
            (f"{key}%", f"%{key}%", f"%{key}%"),
        ).fetchone()

    def list_series(self) -> list[sqlite3.Row]:
        return self.con.execute("SELECT * FROM series ORDER BY title").fetchall()

    def set_bundle(self, series_id: int, path: str, uuid_: str | None = None) -> None:
        """Record where this series' bundle lives. `path` is absolute and
        machine-local, which is correct — the state DB is machine-local too."""
        if uuid_:
            self.con.execute("UPDATE series SET path=?, uuid=? WHERE id=?",
                             (path, uuid_, series_id))
        else:
            self.con.execute("UPDATE series SET path=? WHERE id=?", (path, series_id))
        self.con.commit()

    def by_uuid(self, uuid_: str):
        return self.con.execute("SELECT * FROM series WHERE uuid=?", (uuid_,)).fetchone()

    def summary(self, key: str | None = None) -> list[dict]:
        """One dict per tracked series: per-stage counts, next/last chapter."""
        rows = [self.get_series(key)] if key else self.list_series()
        out: list[dict] = []
        for s in filter(None, rows):
            chs = self.chapters(s["id"])
            by_stage = {st: sum(1 for c in chs if c["status"] == st) for st in STATUSES}
            rendered = [c for c in chs if c["status"] == "rendered"]
            pending = self.pending(s["id"])
            nxt = pending[0] if pending else None
            last = rendered[-1] if rendered else None
            keys = s.keys()

            def _jlist(col):
                try:
                    return json.loads(s[col]) if col in keys and s[col] else []
                except (ValueError, TypeError):
                    return []

            out.append({
                "slug": s["slug"], "title": s["title"], "author": s["author"],
                "url": s["url"], "rr_id": s["rr_id"],
                "provider": s["provider"] if "provider" in keys else "royalroad",
                "enabled": bool(s["enabled"]) if "enabled" in keys else True,
                "tags": _jlist("tags"), "warnings": _jlist("warnings"),
                "status": (s["status"] if "status" in keys else "") or "",
                "rating": (s["rating"] if "rating" in keys else 0.0) or 0.0,
                "chapters": len(chs), "rendered": len(rendered),
                "stages": by_stage,
                "pending": len(pending), "errors": by_stage["error"],
                "next": {"number": nxt["ord"] + 1, "title": nxt["title"]} if nxt else None,
                "last_rendered": {"number": last["ord"] + 1, "title": last["title"],
                                  "at": last["rendered_at"]} if last else None,
                "added_at": s["added_at"],
                "uuid": s["uuid"] if "uuid" in keys else None,
                "path": s["path"] if "path" in keys else None,
            })
        return out

    # -- volumes ---------------------------------------------------------
    def replace_volumes(self, series_id: int, volumes) -> int:
        """Upsert the volume list. Volumes are optional and often incomplete:
        Royal Road authors assign them per chapter, so a fiction can have none
        at all, or leave most chapters unassigned (Spector: 529 of 746). A
        chapter with no volume is normal, not an error."""
        for v in volumes or []:
            self.con.execute(
                """INSERT INTO volumes (series_id, rr_id, title, cover_url, ord)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(series_id, rr_id) DO UPDATE SET
                     title=excluded.title, cover_url=excluded.cover_url,
                     ord=excluded.ord""",
                (series_id, str(v.rr_id), v.title, v.cover_url, v.order))
        self.con.commit()
        return len(volumes or [])

    def volumes(self, series_id: int) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT * FROM volumes WHERE series_id=? ORDER BY ord", (series_id,)
        ).fetchall()

    def volume_map(self, series_id: int) -> dict:
        """rr_id -> row, plus a 1-based `index` that IS contiguous (RR's `ord`
        is not — Sky Pride runs 1,2,3,4,6,7)."""
        out = {}
        for i, v in enumerate(self.volumes(series_id), 1):
            out[v["rr_id"]] = {"row": v, "index": i, "title": v["title"],
                               "cover_url": v["cover_url"]}
        return out

    # -- chapters --------------------------------------------------------
    def replace_chapters(self, series_id: int, chapters) -> int:
        """Upsert chapter rows (keeps status / audio_path). Returns how many are new."""
        existing = {
            r["rr_id"] for r in self.con.execute(
                "SELECT rr_id FROM chapters WHERE series_id=?", (series_id,)
            )
        }
        # 1-based position within each volume, in chapter order
        seq, counts = {}, {}
        for c in sorted(chapters, key=lambda c: c.order):
            vid = getattr(c, "volume_id", None)
            counts[vid] = counts.get(vid, 0) + 1
            seq[c.rr_id] = counts[vid] if vid else None
        for c in chapters:
            self.con.execute(
                """INSERT INTO chapters
                     (series_id, rr_id, ord, title, slug, url, published_at, unlocked,
                      volume_rr_id, volume_chapter)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(series_id, rr_id) DO UPDATE SET
                     ord=excluded.ord, title=excluded.title, slug=excluded.slug,
                     url=excluded.url, published_at=excluded.published_at,
                     unlocked=excluded.unlocked,
                     volume_rr_id=excluded.volume_rr_id,
                     volume_chapter=excluded.volume_chapter""",
                (series_id, c.rr_id, c.order, c.title, c.slug, c.url,
                 c.published_at, int(c.unlocked),
                 str(c.volume_id) if getattr(c, "volume_id", None) else None,
                 seq.get(c.rr_id)),
            )
        self.con.commit()
        return sum(1 for c in chapters if c.rr_id not in existing)

    def chapters(self, series_id: int) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT * FROM chapters WHERE series_id=? ORDER BY ord", (series_id,)
        ).fetchall()

    def select(self, series_id: int, spans=None) -> list[sqlite3.Row]:
        """Chapters matching any of `spans` (1-based, `None` = open end), in
        chapter order — the *imperative* set.

        An explicit selection means "do these", so nothing is filtered out: even
        `skipped` and `rendered` chapters come back. No spans = every chapter.
        """
        rows = self.chapters(series_id)
        if not spans:
            return rows
        out, seen = [], set()
        for lo, hi in spans:
            for c in rows:
                n = c["ord"] + 1
                if (lo is None or n >= lo) and (hi is None or n <= hi) \
                        and c["id"] not in seen:
                    seen.add(c["id"])
                    out.append(c)
        return sorted(out, key=lambda c: c["ord"])

    def range(self, series_id: int, lo: int | None = None,
              hi: int | None = None) -> list[sqlite3.Row]:
        """Back-compat single-span wrapper around `select`."""
        return self.select(series_id, [(lo, hi)] if (lo or hi) else None)

    def outstanding(self, series_id: int, stage: str = "rendered",
                    limit: int | None = None) -> list[sqlite3.Row]:
        """Unlocked chapters not yet at `stage` — the *declarative* set.

        `skipped` is honoured (that's its whole job) and `error` is retried.
        """
        want = stage_rank(stage)
        rows = [
            c for c in self.chapters(series_id)
            if c["unlocked"] and c["status"] != "skipped"
            and (c["status"] == "error" or stage_rank(c["status"]) < want)
        ]
        return rows[:limit] if limit else rows

    # kept as the name the feed/serve/report paths use for "still to render"
    def pending(self, series_id: int, limit: int | None = None) -> list[sqlite3.Row]:
        return self.outstanding(series_id, "rendered", limit)

    _STAGE_FIELD = {"fetched": "fetched_at", "parsed": "parsed_at",
                    "rendered": "rendered_at"}

    def mark_stage(self, chapter_id: int, stage: str, **kw) -> None:
        """Record that `stage` completed, without ever moving a chapter backwards.

        Re-running an earlier stage on a finished chapter (`parse` on something
        already rendered) must not demote it — the .opus is still there, and the
        status is "furthest stage reached", not "last stage run". Timestamps and
        paths are still updated, so the re-run is recorded.
        """
        row = self.con.execute("SELECT status FROM chapters WHERE id=?",
                               (chapter_id,)).fetchone()
        cur = row["status"] if row else "new"
        keep = cur if stage_rank(cur) > stage_rank(stage) else stage
        self.mark(chapter_id, keep, _stage_time=stage, **kw)

    def mark(self, chapter_id: int, status: str, *, raw_path: str | None = None,
             text_path: str | None = None, audio_path: str | None = None,
             duration_s: float | None = None, error: str | None = None,
             error_stage: str | None = None, narrator: str | None = None,
             render_started_at: str | None = None,
             render_ended_at: str | None = None,
             synth_fingerprint: str | None = None,
             _stage_time: str | None = None) -> None:
        """Advance (or reset) one chapter. Only the fields you pass are touched —
        a re-render must not erase the raw/text paths from earlier stages."""
        sets = ["status=?", "error=?", "error_stage=?"]
        args: list = [status, error, error_stage]
        for col, val in (("raw_path", raw_path), ("text_path", text_path),
                         ("audio_path", audio_path), ("duration_s", duration_s),
                         ("narrator", narrator),
                         ("synth_fingerprint", synth_fingerprint),
                         ("render_started_at", render_started_at),
                         ("render_ended_at", render_ended_at)):
            if val is not None:
                sets.append(f"{col}=?")
                args.append(val)
        field = self._STAGE_FIELD.get(_stage_time or status)
        if field:
            sets.append(f"{field}=?")
            args.append(time.strftime("%Y-%m-%dT%H:%M:%S"))
        args.append(chapter_id)
        self.con.execute(f"UPDATE chapters SET {', '.join(sets)} WHERE id=?", args)
        self.con.commit()

    def render_samples(self, series_id: int) -> list[tuple[float, float]]:
        """[(audio_seconds, wall_seconds)] for chapters we actually timed.

        Feeds the cost model in `sync.estimate_render`, so the estimate tracks
        this machine and this series rather than a baked-in constant.
        """
        out = []
        for c in self.chapters(series_id):
            if not (c["render_started_at"] and c["render_ended_at"] and c["duration_s"]):
                continue
            try:
                a = time.mktime(time.strptime(c["render_started_at"], "%Y-%m-%dT%H:%M:%S"))
                b = time.mktime(time.strptime(c["render_ended_at"], "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, TypeError):
                continue
            if b > a:
                out.append((float(c["duration_s"]), b - a))
        return out

    def set_status(self, chapter_ids, status: str) -> int:
        """Bulk status set, for `state set` / `state reset`."""
        ids = list(chapter_ids)
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        self.con.execute(
            f"UPDATE chapters SET status=?, error=NULL, error_stage=NULL WHERE id IN ({q})",
            [status, *ids])
        self.con.commit()
        return len(ids)

    def set_enabled(self, series_id: int, enabled: bool) -> None:
        self.con.execute("UPDATE series SET enabled=? WHERE id=?",
                         (1 if enabled else 0, series_id))
        self.con.commit()

    def forget(self, series_id: int) -> None:
        self.con.execute("DELETE FROM chapters WHERE series_id=?", (series_id,))
        self.con.execute("DELETE FROM series WHERE id=?", (series_id,))
        self.con.commit()

    def close(self) -> None:
        self.con.close()
