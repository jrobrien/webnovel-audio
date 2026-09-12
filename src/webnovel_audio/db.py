"""SQLite library state: tracked series and the per-chapter pipeline stage.

A chapter walks one ordered path — new -> fetched -> parsed -> rendered — plus
two off-path states: `error` (with `error_stage` naming which stage broke, so a
render failure doesn't lose the fact that it *was* fetched and parsed) and
`skipped` (deliberately not wanted: behind `series add --from N`, or set by
hand). `status` alone decides what work is outstanding. The series-level
`progress_order` is *only* "how far the reader has got" — it never gates work,
which is the bug it used to cause when it doubled as a render high-water mark.
"""
from __future__ import annotations

import os
import sqlite3
import time

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
    progress_order INTEGER DEFAULT -1,   -- reader position only; does NOT gate work
    enabled        INTEGER DEFAULT 1,    -- 0 = excluded from `sync`
    added_at       TEXT
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
    error        TEXT,
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
                           ("fetched_at", "TEXT"), ("parsed_at", "TEXT")):
            if name not in cols:
                self.con.execute(f"ALTER TABLE chapters ADD COLUMN {name} {decl}")
        scols = {r["name"] for r in self.con.execute("PRAGMA table_info(series)")}
        if "provider" not in scols:
            self.con.execute("ALTER TABLE series ADD COLUMN provider TEXT DEFAULT 'royalroad'")
        if "enabled" not in scols:
            self.con.execute("ALTER TABLE series ADD COLUMN enabled INTEGER DEFAULT 1")

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

    # -- series ------------------------------------------------------------
    def upsert_series(self, fi) -> int:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        cur = self.con.execute(
            """INSERT INTO series (rr_id, provider, slug, title, author, url, cover_url, added_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(rr_id) DO UPDATE SET
                 provider=excluded.provider, slug=excluded.slug, title=excluded.title,
                 author=excluded.author, url=excluded.url, cover_url=excluded.cover_url""",
            (fi.rr_id, getattr(fi, "provider", "royalroad"), fi.slug, fi.title,
             fi.author, fi.url, fi.cover_url, now),
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
            out.append({
                "slug": s["slug"], "title": s["title"], "author": s["author"],
                "url": s["url"], "rr_id": s["rr_id"],
                "provider": s["provider"] if "provider" in keys else "royalroad",
                "enabled": bool(s["enabled"]) if "enabled" in keys else True,
                "chapters": len(chs), "rendered": len(rendered),
                "stages": by_stage,
                "pending": len(pending), "errors": by_stage["error"],
                "progress": s["progress_order"] + 1,
                "next": {"number": nxt["ord"] + 1, "title": nxt["title"]} if nxt else None,
                "last_rendered": {"number": last["ord"] + 1, "title": last["title"],
                                  "at": last["rendered_at"]} if last else None,
                "added_at": s["added_at"],
            })
        return out

    def set_progress(self, series_id: int, order: int) -> None:
        self.con.execute(
            "UPDATE series SET progress_order=MAX(progress_order, ?) WHERE id=?",
            (order, series_id),
        )
        self.con.commit()

    def force_progress(self, series_id: int, order: int) -> None:
        self.con.execute(
            "UPDATE series SET progress_order=? WHERE id=?", (order, series_id)
        )
        self.con.commit()

    # -- chapters --------------------------------------------------------
    def replace_chapters(self, series_id: int, chapters) -> int:
        """Upsert chapter rows (keeps status / audio_path). Returns how many are new."""
        existing = {
            r["rr_id"] for r in self.con.execute(
                "SELECT rr_id FROM chapters WHERE series_id=?", (series_id,)
            )
        }
        for c in chapters:
            self.con.execute(
                """INSERT INTO chapters
                     (series_id, rr_id, ord, title, slug, url, published_at, unlocked)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(series_id, rr_id) DO UPDATE SET
                     ord=excluded.ord, title=excluded.title, slug=excluded.slug,
                     url=excluded.url, published_at=excluded.published_at,
                     unlocked=excluded.unlocked""",
                (series_id, c.rr_id, c.order, c.title, c.slug, c.url,
                 c.published_at, int(c.unlocked)),
            )
        self.con.commit()
        return sum(1 for c in chapters if c.rr_id not in existing)

    def chapters(self, series_id: int) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT * FROM chapters WHERE series_id=? ORDER BY ord", (series_id,)
        ).fetchall()

    def range(self, series_id: int, lo: int | None = None,
              hi: int | None = None) -> list[sqlite3.Row]:
        """Chapters by 1-based number, whatever their status — the *imperative* set.

        An explicit range means "do these", so nothing is filtered out here; even
        `skipped` and `rendered` chapters come back.
        """
        sql = "SELECT * FROM chapters WHERE series_id=?"
        args: list = [series_id]
        if lo is not None:
            sql += " AND ord >= ?"
            args.append(lo - 1)
        if hi is not None:
            sql += " AND ord <= ?"
            args.append(hi - 1)
        return self.con.execute(sql + " ORDER BY ord", args).fetchall()

    def outstanding(self, series_id: int, stage: str = "rendered",
                    limit: int | None = None) -> list[sqlite3.Row]:
        """Unlocked chapters not yet at `stage` — the *declarative* set.

        Status alone decides; the reader's `progress_order` is deliberately not
        consulted. `skipped` is honoured (that's its whole job), `error` is
        retried.
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

    def mark(self, chapter_id: int, status: str, *, raw_path: str | None = None,
             text_path: str | None = None, audio_path: str | None = None,
             duration_s: float | None = None, error: str | None = None,
             error_stage: str | None = None) -> None:
        """Advance (or reset) one chapter. Only the fields you pass are touched —
        a re-render must not erase the raw/text paths from earlier stages."""
        sets = ["status=?", "error=?", "error_stage=?"]
        args: list = [status, error, error_stage]
        for col, val in (("raw_path", raw_path), ("text_path", text_path),
                         ("audio_path", audio_path), ("duration_s", duration_s)):
            if val is not None:
                sets.append(f"{col}=?")
                args.append(val)
        field = self._STAGE_FIELD.get(status)
        if field:
            sets.append(f"{field}=?")
            args.append(time.strftime("%Y-%m-%dT%H:%M:%S"))
        args.append(chapter_id)
        self.con.execute(f"UPDATE chapters SET {', '.join(sets)} WHERE id=?", args)
        self.con.commit()

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
