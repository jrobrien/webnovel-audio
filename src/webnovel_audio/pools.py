"""Named resource pools: the locks jobs queue on instead of refusing.

One state DB is one library, and the jobs run against it (`render`, `sync`,
`fetch`, `parse`, `grab`, `cache`, `series rename --move`) share a few real
resources. Each is a named pool, an `flock` on a file next to the DB:

| pool              | kind                 | who, and for how long                       |
|-------------------|----------------------|---------------------------------------------|
| `library`         | shared / exclusive   | every job shared, for the whole command;    |
|                   |                      | rename --move and cache clear/prune         |
|                   |                      | exclusive (they move or delete files)       |
| `browser`         | exclusive            | `grab`, for the whole run (one extension)   |
| `synth`           | exclusive            | one chapter's synthesis + assembly (it uses |
|                   |                      | every core); not its mastering              |
| `net-<provider>`  | exclusive            | one page fetch from that site               |

`library` is the old `sync.lock` file, so a tool that probes that still works;
the others are `locks/<name>.lock`.

**Lock order**, which rules out deadlock: `library` -> `browser` -> `synth` ->
`net-*`. A thread never waits for an earlier pool while holding a later one;
`hold` raises `RuntimeError` if asked to. Chapter claims (`claim_chapter`) sit
outside the order because they are never waited on.

`flock` is advisory and per open file: it goes away the moment the holding
process exits, crash or not, so there is never a stale lock to clean up. A
holder writes one JSON line (pid, command, what, since) into the file once it
has the lock, so a waiter or `progress` can say who it is waiting for. The file
is opened without truncating (`a+`) and only rewritten by a holder, so a waiter
opening it never erases the holder's record.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import threading
import time

ORDER = ("library", "browser", "synth", "net")
LIBRARY = "library"

#: the subcommand this process runs, for holder records ("waiting for synth
#: (held by grab, ...)"); `cli.main` sets it
COMMAND = ""

_local = threading.local()          # pools held by this thread: [(name, shared)]


class PoolBusy(Exception):
    """A pool is held by another job and the caller asked not to wait.

    `sync.SyncLocked` is this class, so `except sync.SyncLocked` catches a busy
    pool of any name."""

    def __init__(self, pool: str, holder: dict | None = None, path: str = ""):
        self.pool, self.holder, self.path = pool, holder or {}, path
        super().__init__(f"{pool} is busy ({describe(self.holder)})"
                         + (f" (lock: {path})" if path else ""))


def _rank(name: str) -> int:
    base = "net" if name.startswith("net-") else name
    return ORDER.index(base) if base in ORDER else len(ORDER)


def state_dir(cfg) -> str:
    db_path = os.path.abspath(os.path.expanduser(cfg.library.state_db))
    return os.path.dirname(db_path) or "."


def lock_path(cfg, name: str) -> str:
    if name == LIBRARY:
        return os.path.join(state_dir(cfg), "sync.lock")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    return os.path.join(state_dir(cfg), "locks", f"{safe}.lock")


def net_pool(provider: str) -> str:
    return f"net-{provider or 'unknown'}"


def describe(holder: dict | None) -> str:
    """'held by grab, pid 123, since 14:02' (or 'holder unknown')."""
    if not holder:
        return "holder unknown"
    since = holder.get("since", "")
    hhmm = since[11:16] if len(since) >= 16 else since
    who = holder.get("what") or holder.get("command") or "another job"
    parts = [f"held by {who}"]
    if holder.get("pid"):
        parts.append(f"pid {holder['pid']}")
    if hhmm:
        parts.append(f"since {hhmm}")
    return ", ".join(parts)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return True
    return True


def read_holders(path: str) -> list[dict]:
    """The holder records in a lock file, live processes only. Never locks."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict) and _alive(rec.get("pid")):
            out.append(rec)
    return out


@contextlib.contextmanager
def _records_mutex(cfg):
    """Serialises rewrites of a shared pool's records (several holders at once).
    A leaf lock held for microseconds, never while waiting on anything else."""
    path = os.path.join(state_dir(cfg), "locks", "records.mutex")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _rewrite(fh, recs: list[dict]) -> None:
    fh.seek(0)
    fh.truncate()
    fh.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))
    fh.flush()


def _parse_lines(text: str) -> list[dict]:
    out = []
    for ln in text.splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _write_record(cfg, fh, rec: dict, shared: bool) -> None:
    if not shared:
        _rewrite(fh, [rec])                 # we are the only holder
        return
    with _records_mutex(cfg):
        fh.seek(0)
        keep = [r for r in _parse_lines(fh.read())
                if _alive(r.get("pid")) and r.get("id") != rec["id"]]
        _rewrite(fh, keep + [rec])


def _clear_record(cfg, fh, rec: dict, shared: bool) -> None:
    """Drop our record while still holding the lock, so the next holder's is
    never erased by us."""
    try:
        if not shared:
            _rewrite(fh, [])
            return
        with _records_mutex(cfg):
            fh.seek(0)
            _rewrite(fh, [r for r in _parse_lines(fh.read())
                          if r.get("id") != rec["id"] and _alive(r.get("pid"))])
    except OSError:
        pass


def _held() -> list:
    if not hasattr(_local, "held"):
        _local.held = []
    return _local.held


@contextlib.contextmanager
def hold(cfg, name: str, *, what: str = "", wait: bool = True, shared: bool = False,
         emit=None, log=None):
    """Hold pool `name` for the body of the `with`.

    Tries without blocking first. When the pool is busy: `wait=False` raises
    `PoolBusy` naming the holder; otherwise emits `{"event": "waiting", "pool",
    "holder"}`, logs who it is waiting for, blocks, and emits `{"event":
    "acquired", "pool", "waited_s"}` once it has it. Ctrl-C while blocked
    raises KeyboardInterrupt as usual. `shared` is for `library` only in
    practice: many shared holders at once, an exclusive one alone.
    """
    held = _held()
    for h, s in held:
        if h == name and wait and not (shared and s):
            raise RuntimeError(f"pool {name!r} is already held by this thread")
        if _rank(h) > _rank(name):
            raise RuntimeError(f"lock order: {name!r} taken while holding {h!r} "
                               f"(order is {' -> '.join(ORDER)})")
    path = lock_path(cfg, name)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    try:
        try:
            fcntl.flock(fh, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            holders = read_holders(path)
            holder = holders[0] if holders else {}
            if not wait:
                raise PoolBusy(name, holder, path) from None
            if emit:
                emit({"event": "waiting", "pool": name, "holder": holder,
                      "what": what})
            if log:
                log(f"waiting for {name} ({describe(holder)})")
            t0 = time.monotonic()
            fcntl.flock(fh, mode)            # KeyboardInterrupt propagates out of here
            waited = time.monotonic() - t0
            if emit:
                emit({"event": "acquired", "pool": name, "waited_s": round(waited, 1)})
            if log:
                log(f"got {name} after {waited:.0f}s")
    except BaseException:
        fh.close()
        raise
    rec = {"id": secrets.token_hex(6), "pid": os.getpid(), "command": COMMAND,
           "what": what, "since": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "mode": "shared" if shared else "exclusive"}
    try:
        _write_record(cfg, fh, rec, shared)
    except OSError:
        pass                                 # the record is informational only
    held.append((name, shared))
    try:
        yield rec
    finally:
        held.remove((name, shared))
        _clear_record(cfg, fh, rec, shared)
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def maybe(cfg, name: str, enabled: bool, **kw):
    """`hold(...)` when `enabled`, else a no-op context."""
    return hold(cfg, name, **kw) if enabled else contextlib.nullcontext()


def check_free(cfg, name: str, *, shared: bool = False) -> None:
    """Raise PoolBusy if `name` is held now (take it and let go at once): what
    `--no-wait` checks up front before starting work it could not finish."""
    with hold(cfg, name, wait=False, shared=shared):
        pass


# -- chapter claims -------------------------------------------------------------
#
# Two runs over the same series compute their work list up front, so both would
# render the same chapters. A run claims a chapter (a byte-range lock at offset
# = chapter id on one file) before working on it and keeps the claim until the
# chapter is settled, mastering included; a declarative run skips a chapter
# another run has claimed. Never waited on, so outside the lock order.

_claim_fd: int | None = None
_claim_path = ""
_claim_mx = threading.Lock()


def _claims_fd(cfg) -> int:
    """One descriptor per process: POSIX record locks drop when *any* descriptor
    of the file is closed by the process, so it is opened once and kept."""
    global _claim_fd, _claim_path
    path = os.path.join(state_dir(cfg), "locks", "chapters.lock")
    if _claim_fd is None or _claim_path != path:
        if _claim_fd is not None:            # another library (tests): its claims go too
            os.close(_claim_fd)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _claim_fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        _claim_path = path
    return _claim_fd


def claim_chapter(cfg, chapter_id: int) -> bool:
    """Claim a chapter for this process. False if another process has it."""
    with _claim_mx:
        fd = _claims_fd(cfg)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, int(chapter_id))
        except OSError:
            return False
        return True


def release_chapter(cfg, chapter_id: int) -> None:
    with _claim_mx:
        try:
            fcntl.lockf(_claims_fd(cfg), fcntl.LOCK_UN, 1, int(chapter_id))
        except OSError:
            pass


# -- status, for `progress` -------------------------------------------------------

def _proc_locks() -> list[dict] | None:
    """Parsed /proc/locks (Linux): who holds and who waits, without touching any
    lock. None where it is not available."""
    try:
        with open("/proc/locks", encoding="ascii", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    out = []
    for ln in lines:
        f = ln.split()
        if len(f) < 8:
            continue
        waiting = f[1] == "->"
        if waiting:
            f = f[:1] + f[2:]
        if f[1] != "FLOCK":
            continue
        try:
            maj, mnr, ino = f[5].split(":")
            out.append({"waiting": waiting, "mode": f[3], "pid": int(f[4]),
                        "dev": (int(maj, 16), int(mnr, 16)), "ino": int(ino)})
        except ValueError:
            continue
    return out


def _probe(path: str) -> bool:
    """Fallback without /proc/locks: is the file locked by someone?"""
    try:
        fh = open(path, "r")
    except OSError:
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    finally:
        fh.close()


def known_pools(cfg) -> list[str]:
    names = ["library", "browser", "synth"]
    d = os.path.join(state_dir(cfg), "locks")
    try:
        names += sorted(n[:-5] for n in os.listdir(d)
                        if n.startswith("net-") and n.endswith(".lock"))
    except OSError:
        pass
    return names


def status(cfg) -> list[dict]:
    """Every pool: free, or held by whom since when, and how many wait for it.
    Read-only: /proc/locks where there is one, else a take-and-drop probe."""
    locks = _proc_locks()
    out = []
    for name in known_pools(cfg):
        path = lock_path(cfg, name)
        entry = {"pool": name, "held": False, "holders": [], "waiting": None}
        try:
            st = os.stat(path)
        except OSError:
            out.append(entry)
            continue
        if locks is not None:
            # by inode alone: on btrfs `stat` reports a per-subvolume device
            # that /proc/locks does not, and a clash across filesystems is rare
            mine = [x for x in locks if x["ino"] == st.st_ino]
            entry["held"] = any(not x["waiting"] for x in mine)
            entry["waiting"] = sum(1 for x in mine if x["waiting"])
        else:
            entry["held"] = _probe(path)
        if entry["held"]:
            entry["holders"] = read_holders(path)
        out.append(entry)
    return out
