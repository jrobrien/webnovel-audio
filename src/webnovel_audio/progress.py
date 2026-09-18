"""What a sync is doing *right now*, read-only.

`state.json` is a post-run export — `bundle.sync_bundle` only runs after the
whole target loop (`sync.run_stage`), so it is stale for the entire duration of
a run and useless for watching one. The state DB is the live surface: it is in
WAL mode (`db.py`) and every chapter row commits as that chapter finishes, so a
second process can read exact per-stage timing while the render is still going.

That is what this module exposes. Everything here opens the database
**read-only** (`mode=ro`) so `progress` can never write, never migrate, and
never block the sync it is watching.

Run boundaries are inferred here, not recorded: there is no `runs` table yet, so
"this run" means the contiguous chain of renders working backwards from the most
recent, broken by a gap longer than `RUN_GAP_SECONDS`. That is a heuristic and
it will mis-join two syncs started minutes apart; recording real run rows is the
next step and would make this exact.
"""
from __future__ import annotations

import os
import sqlite3
import time

from .config import Config
from .sync import (MIN_COST_SAMPLES, RENDER_COST_RATIO, _FALLBACK_CHAPTER_SECONDS,
                   _dir_slug, _lock_path, human_duration)

TS = "%Y-%m-%dT%H:%M:%S"          # what db.py writes, in local time
RUN_GAP_SECONDS = 900.0           # a quiet stretch this long ends a "run"
RECENT_DEFAULT = 8


def _epoch(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        return time.mktime(time.strptime(stamp, TS))
    except (ValueError, TypeError):
        return None


def open_ro(cfg: Config) -> sqlite3.Connection:
    """A read-only handle on the state DB.

    Deliberately not `db.DB`: that opens read-write and runs `_migrate()`,
    which can ALTER TABLE. A monitoring command must not be able to do that,
    least of all while a sync holds the database.
    """
    path = os.path.abspath(os.path.expanduser(cfg.royalroad.state_db))
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def sync_running(cfg: Config) -> bool:
    """Is a sync holding the lock? Probe it without disturbing the holder.

    `flock` is advisory and per-open-file-description: taking it and dropping
    it immediately tells us whether someone else has it, and if nobody does we
    have held it for microseconds in a process that is about to let go.
    """
    import fcntl

    path = _lock_path(cfg)
    if not os.path.exists(path):
        return False
    try:
        fh = open(path, "r")
    except OSError:
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True                       # somebody else holds it: a sync is live
    else:
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    finally:
        fh.close()


def _series_rows(con, key: str | None):
    rows = con.execute("SELECT * FROM series ORDER BY title").fetchall()
    if key is None:
        return [s for s in rows if s["enabled"]]
    k = key.lower()
    hit = [s for s in rows
           if k in (str(s["rr_id"]), (s["title"] or "").lower(), _dir_slug(s).lower())]
    return hit or [s for s in rows if k in (s["title"] or "").lower()]


def _chapter_cost(con, series_id: int) -> tuple[float, float]:
    """(median audio seconds, wall/audio ratio) for this series.

    Mirrors `sync.estimate_render`'s model — the constants are imported rather
    than copied, but the two will drift if one is changed alone. Folding both
    onto one helper is worth doing when the `runs` table lands.
    """
    import statistics

    rows = con.execute("SELECT * FROM chapters WHERE series_id=?", (series_id,)).fetchall()
    known = [c["duration_s"] for c in rows if c["status"] == "rendered" and c["duration_s"]]
    per_ch = statistics.median(known) if known else _FALLBACK_CHAPTER_SECONDS
    timed = []
    for c in rows:
        a, b = _epoch(c["render_started_at"]), _epoch(c["render_ended_at"])
        if a and b and b > a and c["duration_s"]:
            timed.append((c["duration_s"], b - a))
    if len(timed) >= MIN_COST_SAMPLES:
        ratio = statistics.median(w / a for a, w in timed if a > 0)
    else:
        ratio = RENDER_COST_RATIO
    return per_ch, ratio


def _outstanding(con, series_id: int) -> int:
    """Same predicate as `db.outstanding` for stage 'rendered'."""
    row = con.execute(
        "SELECT COUNT(*) n FROM chapters WHERE series_id=? AND unlocked=1 "
        "AND status != 'skipped' AND status != 'rendered'", (series_id,)).fetchone()
    return row["n"]


def snapshot(cfg: Config, key: str | None = None, *,
             recent: int = RECENT_DEFAULT) -> dict:
    """Everything `progress` knows, as plain data (also the --json payload)."""
    con = open_ro(cfg)
    try:
        now = time.time()
        targets = _series_rows(con, key)
        by_id = {s["id"]: s for s in targets}

        started, done, errors = [], [], []
        for s in targets:
            for c in con.execute(
                    "SELECT * FROM chapters WHERE series_id=? AND render_started_at "
                    "IS NOT NULL ORDER BY render_started_at DESC LIMIT 400",
                    (s["id"],)).fetchall():
                (done if c["render_ended_at"] else started).append(c)
            for c in con.execute(
                    "SELECT * FROM chapters WHERE series_id=? AND status='error'",
                    (s["id"],)).fetchall():
                errors.append(c)

        done.sort(key=lambda c: _epoch(c["render_ended_at"]) or 0, reverse=True)

        # Infer the current run: walk back while the quiet gap between one
        # chapter ending and the next starting stays under RUN_GAP_SECONDS.
        run: list[sqlite3.Row] = []
        prev_start = None
        for c in done:
            end = _epoch(c["render_ended_at"])
            if prev_start is not None and end is not None \
                    and prev_start - end > RUN_GAP_SECONDS:
                break
            run.append(c)
            prev_start = _epoch(c["render_started_at"])
        run_started = min((_epoch(c["render_started_at"]) or now for c in run),
                          default=None)

        in_flight = []
        for c in started:
            t0 = _epoch(c["render_started_at"])
            in_flight.append({
                "slug": _dir_slug(by_id[c["series_id"]]), "number": c["ord"] + 1,
                "title": c["title"], "started_at": c["render_started_at"],
                "elapsed_seconds": round(now - t0, 1) if t0 else None})

        per_series = []
        for s in targets:
            outstanding = _outstanding(con, s["id"])
            per_ch, ratio = _chapter_cost(con, s["id"])
            in_run = sum(1 for c in run if c["series_id"] == s["id"])
            per_series.append({
                "slug": _dir_slug(s), "title": s["title"],
                "outstanding": outstanding, "done_in_run": in_run,
                "seconds_per_chapter": round(per_ch * ratio, 1),
                "eta_seconds": round(outstanding * per_ch * ratio, 1)})

        recents = []
        for c in run[:recent]:
            t0, t1 = _epoch(c["render_started_at"]), _epoch(c["render_ended_at"])
            wall = (t1 - t0) if (t0 and t1) else None
            audio = c["duration_s"]
            recents.append({
                "slug": _dir_slug(by_id[c["series_id"]]), "number": c["ord"] + 1,
                "title": c["title"], "wall_seconds": round(wall, 1) if wall else None,
                "audio_seconds": round(audio, 1) if audio else None,
                "ratio": round(wall / audio, 3) if (wall and audio) else None,
                "fetched_at": c["fetched_at"], "parsed_at": c["parsed_at"],
                "rendered_at": c["rendered_at"],
                "narrator": c["narrator"], "fingerprint": c["synth_fingerprint"]})

        run_wall = (now - run_started) if run_started else 0.0
        run_audio = sum(c["duration_s"] or 0 for c in run)
        return {
            "now": time.strftime(TS),
            "running": sync_running(cfg),
            "lock": _lock_path(cfg),
            "run": {"started_at": time.strftime(TS, time.localtime(run_started))
                    if run_started else None,
                    "chapters": len(run), "wall_seconds": round(run_wall, 1),
                    "audio_seconds": round(run_audio, 1),
                    "chapters_per_hour": round(len(run) / (run_wall / 3600), 2)
                    if run_wall > 60 else None,
                    "inferred": True},
            "in_flight": in_flight,
            "series": per_series,
            "recent": recents,
            "errors": [{"slug": _dir_slug(by_id[c["series_id"]]), "number": c["ord"] + 1,
                        "title": c["title"], "stage": c["error_stage"],
                        "error": (c["error"] or "")[:120]} for c in errors],
            "totals": {"outstanding": sum(s["outstanding"] for s in per_series),
                       "eta_seconds": sum(s["eta_seconds"] for s in per_series)},
        }
    finally:
        con.close()


def render_text(snap: dict) -> str:
    """The human view. One screen, newest first."""
    out: list[str] = []
    state = "sync RUNNING" if snap["running"] else "idle (no sync holds the lock)"
    out.append(f"{snap['now']}   {state}")

    r = snap["run"]
    if r["chapters"]:
        rate = f", {r['chapters_per_hour']}/h" if r["chapters_per_hour"] else ""
        out.append(f"run (inferred): {r['chapters']} chapter(s) since {r['started_at']}"
                   f" — {human_duration(r['wall_seconds'])} wall, "
                   f"{human_duration(r['audio_seconds'])} audio{rate}")

    if snap["in_flight"]:
        out.append("\nin flight:")
        for c in snap["in_flight"]:
            el = human_duration(c["elapsed_seconds"]) if c["elapsed_seconds"] else "?"
            out.append(f"  {c['slug']:<18} #{c['number']:<4} {c['title'][:40]:<42} {el:>8}")
    elif snap["running"]:
        out.append("\nin flight: (between chapters — fetching, parsing or mastering)")

    if snap["recent"]:
        out.append("\nrecent:")
        out.append(f"  {'series':<18} {'#':<5} {'title':<42} {'wall':>8} {'audio':>8} {'x':>6}")
        for c in snap["recent"]:
            w = human_duration(c["wall_seconds"]) if c["wall_seconds"] else "-"
            a = human_duration(c["audio_seconds"]) if c["audio_seconds"] else "-"
            x = f"{c['ratio']:.2f}" if c["ratio"] else "-"
            out.append(f"  {c['slug']:<18} #{c['number']:<4} {c['title'][:40]:<42} "
                       f"{w:>8} {a:>8} {x:>6}")

    out.append("\noutstanding:")
    out.append(f"  {'series':<18} {'left':>5} {'this run':>9} {'per ch':>8} {'eta':>10}")
    for s in snap["series"]:
        out.append(f"  {s['slug']:<18} {s['outstanding']:>5} {s['done_in_run']:>9} "
                   f"{human_duration(s['seconds_per_chapter']):>8} "
                   f"{human_duration(s['eta_seconds']):>10}")
    t = snap["totals"]
    out.append(f"  {'TOTAL':<18} {t['outstanding']:>5} {'':>9} {'':>8} "
               f"{human_duration(t['eta_seconds']):>10}")

    if snap["errors"]:
        out.append(f"\nerrors ({len(snap['errors'])}), retried on the next run:")
        for e in snap["errors"][:8]:
            out.append(f"  {e['slug']:<18} #{e['number']:<4} {e['stage'] or '?':<8} {e['error']}")
    return "\n".join(out)
