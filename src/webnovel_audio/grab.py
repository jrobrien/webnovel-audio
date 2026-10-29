"""A fetch -> ingest -> render loop led from this process, the browser loading the pages.

`webnovel-audio grab` runs a small server on the loopback address and opens the
series page in the browser, where the page-grabber extension (extension/) takes
its orders from this process: it sends each page it loads, and asks "what next?".
This process files the page (`sync.ingest_raw`), renders that chapter, and only
answers with the next chapter once the render is done, so fetching is paced by
rendering and the two cannot drift apart. The extension never decides anything.

The server is not a web service: it listens on 127.0.0.1 only, on a random port;
every request must carry the one-time token (given to the browser in the page
address, after the `#`, so it is never sent anywhere) and a Host that names
this loopback address; and a request from a web page, rather than from the
extension, is refused.
"""
from __future__ import annotations

import hmac
import json
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import sync
from .config import Config

MAX_BODY = 16 * 1024 * 1024        # a chapter page is under 1 MB; this is a ceiling

#: what the extension and this server say to each other. Bump it when a message
#: changes shape, together with PROTOCOL in background.js under extension/;
#: a browser running the other number is told so instead of failing obscurely.
PROTOCOL = 1


class GrabLoop:
    def __init__(self, cfg: Config, slug: str, count: int, *, ahead: int = 0,
                 idle_timeout: float = 300.0, emit=None, render=None):
        self.cfg = cfg
        self.slug = slug
        self.count = max(1, count)
        #: fetched-but-not-yet-rendered chapters allowed when the next is released
        self.ahead = max(0, ahead)
        self.idle_timeout = idle_timeout
        self._emit = emit or (lambda _d: None)
        self._render = render or self._render_chapter
        self.token = secrets.token_urlsafe(24)
        self.port = 0
        self.rendered = 0
        self.errors = 0
        self.ingested = 0
        self.finished = ""                 # why the loop is over, once it is
        self._lock = threading.Lock()
        self._emit_lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._queued: set[int] = set()
        self._handed: dict | None = None   # the chapter we sent the browser to fetch
        self._given: set[str] = set()
        self._last_contact = time.monotonic()
        self._httpd: ThreadingHTTPServer | None = None
        db = sync._db(cfg)
        try:
            s = db.get_series(slug)
            if s is None:
                raise SystemExit(f"no tracked series {slug!r}")
            self.series_id, self.series_url, self.slug = s["id"], s["url"], s["slug"]
            self.title = s["title"]
        finally:
            db.close()

    # -- what the browser is told -----------------------------------------------
    def emit(self, d: dict) -> None:
        with self._emit_lock:
            try:
                self._emit(d)
            except (BrokenPipeError, OSError):      # the UI closed us: stop
                self.finish("stopped (the reader of this output went away)")

    def say(self, message: str) -> None:
        self.emit({"event": "grab", "message": message})

    def finish(self, reason: str) -> None:
        with self._lock:
            if not self.finished:
                self.finished = reason

    def next_action(self) -> dict:
        db = sync._db(self.cfg)
        try:
            with self._lock:
                self._last_contact = time.monotonic()
                if self.finished:
                    return {"action": "done", "reason": self.finished}
                if self._handed:                    # asked again before the page arrived
                    return {"action": "fetch", **self._handed}
                if self.ingested >= self.count:
                    return {"action": "done", "reason": f"{self.count} chapter(s) fetched"}
                unrendered = db.con.execute(
                    "SELECT COUNT(*) FROM chapters WHERE series_id=? "
                    "AND status IN ('fetched','parsed')", (self.series_id,)).fetchone()[0]
                if unrendered > self.ahead:
                    return {"action": "wait", "reason": f"rendering ({unrendered} to go)"}
                row = next((r for r in db.con.execute(
                    "SELECT * FROM chapters WHERE series_id=? AND status='new' "
                    "ORDER BY ord", (self.series_id,)) if r["source_id"] not in self._given),
                    None)
                if row is None:
                    self.finished = "nothing outstanding"
                    return {"action": "done", "reason": self.finished}
                self._given.add(row["source_id"])
                self._handed = {"url": row["url"], "number": row["ord"] + 1,
                                "title": row["title"]}
                return {"action": "fetch", **self._handed}
        finally:
            db.close()

    def take_page(self, html: str) -> dict:
        db = sync._db(self.cfg)
        try:
            r = sync.ingest_raw(self.cfg, db, html)
            queue_it = None
            if r.get("kind") == "chapter" and r["result"] in ("ingested", "have"):
                row = db.con.execute(
                    "SELECT status FROM chapters WHERE series_id=? AND ord=?",
                    (self.series_id, r["number"] - 1)).fetchone()
                if row and row["status"] in ("fetched", "parsed"):
                    queue_it = r["number"]
        finally:
            db.close()
        ok = r["result"] in ("ingested", "have")
        self.say(f"{r['result']}: {r['detail']}")
        with self._lock:
            self._last_contact = time.monotonic()
            if r.get("kind") == "chapter" and ok:
                self._handed = None
                if r["result"] == "ingested":
                    self.ingested += 1
                if queue_it and queue_it not in self._queued:
                    self._queued.add(queue_it)
                    self._queue.put(queue_it)
        if not ok:
            self.finish(f"the browser sent a page that was not usable: {r['detail']}")
        return {"ok": ok, **r}

    # -- rendering, on the calling thread ----------------------------------------
    def _render_emit(self, d: dict) -> None:
        """Each chapter is its own one-chapter run, which announces itself with a
        start, a series line and a done. This process says those once for the whole
        loop, so only the per-chapter events pass."""
        if d.get("event") not in ("start", "series", "done"):
            self.emit(d)

    def _render_chapter(self, number: int):
        return sync.run_stage(self.cfg, "rendered", self.slug, lo=number, hi=number,
                              backend=self.cfg.synth.backend, log=lambda *_: None,
                              emit=self._render_emit)

    def run(self, on_ready=None) -> int:
        """Serve and render until done. `on_ready(url)` is told the address to open."""
        self._httpd = _server(self)
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        self.emit({"event": "start", "stage": "rendered", "series": 1,
                   "dry_run": False, "range": None})
        self.say(f"{self.title}: fetching up to {self.count} chapter(s) through the browser")
        if on_ready:
            on_ready(f"{self.series_url}#wa-run={self.port}:{self.token}")
        beat = time.monotonic()
        try:
            while True:
                try:
                    number = self._queue.get(timeout=0.5)
                except queue.Empty:
                    number = None
                if number is not None:
                    self._render_one(number)
                    continue
                with self._lock:
                    over = bool(self.finished) or self.ingested >= self.count
                if over and self._queue.empty():
                    break
                if time.monotonic() - self._last_contact > self.idle_timeout:
                    self.finish("the browser went quiet")
                    continue
                if time.monotonic() - beat > 15:     # also notices a closed pipe
                    beat = time.monotonic()
                    self.emit({"event": "grab", "message": "waiting…", "quiet": True})
        finally:
            self._httpd.shutdown()
            self._httpd.server_close()
        reason = self.finished or f"{self.count} chapter(s) fetched"
        self.say(f"finished: {reason}")
        self.emit({"event": "done", "stage": "rendered", "done": self.rendered,
                   "errors": self.errors, "skipped": 0})
        return 1 if self.errors else 0

    def _render_one(self, number: int) -> None:
        try:
            res = self._render(number)
            self.rendered += getattr(res, "rendered", 0) or 0
            self.errors += getattr(res, "errors", 0) or 0
        except (Exception, SystemExit) as exc:
            self.errors += 1
            self.say(f"render of #{number} failed: {exc}")


def _server(loop: GrabLoop) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):               # no access log: pages are large
            pass

        def _refuse(self, why: str) -> bool:
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            port = self.server.server_address[1]
            if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                bad = "host"
            elif origin and not origin.startswith(("chrome-extension://", "moz-extension://")):
                bad = "origin"
            elif not hmac.compare_digest(self.headers.get("X-Grab-Token", ""), loop.token):
                bad = "token"
            else:
                bad = ""
            if bad:
                self._send(403, {"error": f"refused ({bad})"})
                return True
            # only a caller holding the token learns anything about versions
            theirs = self.headers.get("X-Grab-Protocol", "")
            if theirs != str(PROTOCOL):
                older = theirs.isdigit() and int(theirs) < PROTOCOL or not theirs
                loop.say("the browser extension is " + ("out of date: reload it at "
                         "chrome://extensions" if older else "newer than this "
                         "webnovel-audio: update webnovel-audio"))
                self._send(426, {"error": "protocol", "server": PROTOCOL,
                                 "extension": theirs or None,
                                 "message": ("reload the extension" if older
                                             else "update webnovel-audio")})
                return True
            return False

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self._refuse("get"):
                return
            if self.path == "/next":
                self._send(200, loop.next_action())
            else:
                self._send(404, {"error": "no such path"})

        def do_POST(self):
            if self._refuse("post"):
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            if self.path == "/page":
                self._send(200, loop.take_page(str(body.get("html", ""))))
            elif self.path == "/stop":
                loop.finish("stopped from the browser")
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "no such path"})

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)
