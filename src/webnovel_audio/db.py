"""SQLite library state: tracked series, their chapters, and render status."""
from __future__ import annotations

import os
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id             INTEGER PRIMARY KEY,
    rr_id          TEXT UNIQUE NOT NULL,
    slug           TEXT,
    title          TEXT,
    author         TEXT,
    url            TEXT,
    cover_url      TEXT,
    progress_order INTEGER DEFAULT -1,   -- highest chapter 'order' already done
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
    status       TEXT DEFAULT 'new',     -- new | rendered | error | skipped
    audio_path   TEXT,
    duration_s   REAL,
    rendered_at  TEXT,
    error        TEXT,
    UNIQUE (series_id, rr_id)
);
"""


class DB:
    def __init__(self, path: str):
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self.con.executescript(_SCHEMA)
        self._migrate()
        self.con.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(chapters)")}
        if "duration_s" not in cols:
            self.con.execute("ALTER TABLE chapters ADD COLUMN duration_s REAL")

    # -- series ------------------------------------------------------------
    def upsert_series(self, fi) -> int:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        cur = self.con.execute(
            """INSERT INTO series (rr_id, slug, title, author, url, cover_url, added_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(rr_id) DO UPDATE SET
                 slug=excluded.slug, title=excluded.title, author=excluded.author,
                 url=excluded.url, cover_url=excluded.cover_url""",
            (fi.rr_id, fi.slug, fi.title, fi.author, fi.url, fi.cover_url, now),
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

    def pending(self, series_id: int, limit: int | None = None) -> list[sqlite3.Row]:
        rows = self.con.execute(
            """SELECT c.* FROM chapters c JOIN series s ON s.id=c.series_id
               WHERE c.series_id=? AND c.unlocked=1 AND c.ord > s.progress_order
                 AND c.status IN ('new', 'error')
               ORDER BY c.ord""",
            (series_id,),
        ).fetchall()
        return rows[:limit] if limit else rows

    def mark(self, chapter_id: int, status: str, *, audio_path: str | None = None,
             duration_s: float | None = None, error: str | None = None) -> None:
        self.con.execute(
            """UPDATE chapters SET status=?, audio_path=?, duration_s=?, error=?, rendered_at=?
               WHERE id=?""",
            (status, audio_path, duration_s, error,
             time.strftime("%Y-%m-%dT%H:%M:%S") if status == "rendered" else None,
             chapter_id),
        )
        self.con.commit()

    def close(self) -> None:
        self.con.close()
