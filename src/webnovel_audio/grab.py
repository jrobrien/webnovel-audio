"""A fetch -> ingest -> render loop led from this process, the browser loading the pages.

`webnovel-audio grab` runs a small server on the loopback address and opens a
series page in the browser, where the page-grabber extension (extension/) takes
its orders from this process: it sends each page it loads, and asks "what next?".
This process files the page (`sync.ingest_raw`), renders that chapter, and only
answers with the next chapter once the render is done, so fetching is paced by
rendering and the two cannot drift apart. The extension never decides anything.

Scope works like `sync`: no scope is every enabled series, in sync's order, and a
paused one is left entirely alone (its chapter list included); a named series is
worked on even if paused. Each series is its own pass: its series page first (the
chapter list, and where the site has you), then up to `limit` chapters.

The server is not a web service: it listens on 127.0.0.1 only, on a random port;
every request must carry the one-time token (given to the browser in the page
address, after the `#`, so it is never sent anywhere) and a Host that names
this loopback address; and a request from a web page, rather than from the
extension, is refused.
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import providers, sync
from .config import Config

MAX_BODY = 16 * 1024 * 1024        # a chapter page is under 1 MB; this is a ceiling

#: what the extension and this server say to each other. Bump it when a message
#: changes shape, together with PROTOCOL in background.js under extension/;
#: a browser running the other number is told so instead of failing obscurely.
PROTOCOL = 2
#: longest a /next request is held open, seconds
MAX_HOLD = 25.0


# -- the extension that ships with this tool -----------------------------------
def extension_dir() -> str | None:
    """The bundled browser extension's folder (the first under extension/)."""
    root = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "extension"))
    try:
        for name in sorted(os.listdir(root)):
            if os.path.exists(os.path.join(root, name, "manifest.json")):
                return os.path.join(root, name)
    except OSError:
        pass
    return None


def extension_version() -> str | None:
    d = extension_dir()
    try:
        with open(os.path.join(d, "manifest.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "") or None
    except (OSError, TypeError, ValueError):
        return None


def version_tuple(v: str | None) -> tuple:
    out = []
    for part in (v or "").split("."):
        out.append(int(part) if part.isdigit() else 0)
    return tuple(out)


def extension_zip() -> bytes:
    """The bundled extension as a zip, for loading it in another browser profile."""
    import io
    import zipfile

    d = extension_dir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, files in os.walk(d):
            for f in sorted(files):
                if f.startswith("."):
                    continue
                full = os.path.join(base, f)
                z.write(full, os.path.join(os.path.basename(d), os.path.relpath(full, d)))
    return buf.getvalue()


_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Page grabber extension</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem}
code{background:#8883;padding:.1em .3em;border-radius:3px}button{font:inherit;padding:.3em .9em}
#status{font-weight:600;margin:1rem 0}.ok{color:#2a7d4f}.warn{color:#b8860b}.bad{color:#b33}</style>
<h1>Page grabber extension</h1>
<p>The browser extension that <code>webnovel-audio grab</code> drives. Bundled with this
tool: version <b id="bundled">@VERSION@</b>.</p>
<p id="status">Checking this browser…</p>
<div id="update" hidden><button id="btn">Update now</button>
<small>(reloads the extension from its folder; open pages are refreshed)</small></div>
<h2>Install</h2>
<ol><li>Open <code>chrome://extensions</code> and turn on <b>Developer mode</b>.</li>
<li>Choose <b>Load unpacked</b> and pick this folder:<br><code>@DIR@</code><br>
(or <a href="/extension.zip">download it as a zip</a>, unpack it and pick that).</li>
<li>Come back to this page: it should say the extension is installed.</li></ol>
<h2>Update</h2>
<p>After updating this tool the folder holds the new files. This page says when the
loaded extension is older, and <b>Update now</b> reloads it. (Or press reload on
<code>chrome://extensions</code>.)</p>
<script>
const bundled = document.getElementById("bundled").textContent;
const tup = v => v.split(".").map(n => parseInt(n, 10) || 0);
const cmp = (a, b) => { a = tup(a); b = tup(b);
  for (let i = 0; i < Math.max(a.length, b.length); i++) { const d = (a[i]||0) - (b[i]||0); if (d) return d; } return 0; };
function check() {
  const have = document.documentElement.dataset.waGrabberVersion;
  const st = document.getElementById("status"), up = document.getElementById("update");
  if (!have) { st.className = "bad"; up.hidden = true;
    st.textContent = "Extension not detected in this browser (open this page at http://localhost:<port>/extension, not another address)."; }
  else if (cmp(have, bundled) < 0) { st.className = "warn"; up.hidden = false;
    st.textContent = "Installed " + have + " is older than the bundled " + bundled + "."; }
  else if (cmp(have, bundled) > 0) { st.className = "warn"; up.hidden = true;
    st.textContent = "Installed " + have + " is newer than this webnovel-audio (" + bundled + "): update webnovel-audio."; }
  else { st.className = "ok"; up.hidden = true; st.textContent = "Installed " + have + ": up to date."; }
}
document.getElementById("btn").onclick = () => {
  window.postMessage({ waGrabber: "reload" }, "*"); setTimeout(() => location.reload(), 2500); };
setTimeout(check, 700);
</script>"""


def extension_page() -> bytes:
    import html
    return (_PAGE.replace("@VERSION@", html.escape(extension_version() or "unknown"))
            .replace("@DIR@", html.escape(extension_dir() or "(extension folder not found)"))
            ).encode()


class GrabLoop:
    def __init__(self, cfg: Config, scope: str | None = None, limit: int | None = None, *,
                 ahead: int = 0, idle_timeout: float = 300.0, stall_timeout: float = 1800.0,
                 emit=None, render=None):
        self.cfg = cfg
        self.limit = limit if limit and limit > 0 else None
        #: fetched-but-not-yet-rendered chapters allowed when the next is released
        self.ahead = max(0, ahead)
        self.idle_timeout = idle_timeout
        #: seconds with a render in flight and nothing finished or begun before the run
        #: gives up on it. Generous: a cold model load or a long chapter is not a stall.
        self.stall_timeout = stall_timeout
        self._emit = emit or (lambda _d: None)
        self._render = render or self._render_chapter
        self._preflight = self._real_preflight if render is None else (lambda: None)
        self._last_progress = time.monotonic()
        self._stalled = False
        self.token = secrets.token_urlsafe(24)
        self.port = 0
        self.rendered = 0
        self.errors = 0
        self.ingested = 0                  # chapters filed this run, all series
        self.finished = ""                 # why the loop is over, once it is
        self._lock = threading.Lock()
        self._emit_lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._queued: set[tuple[int, int]] = set()
        self._inflight = 0                 # chapters queued or rendering, not yet done
        self._contacted = False            # has the extension ever answered?
        self.hint_after = 20.0             # seconds of silence before saying how to install it
        self._hinted_old = False
        self._handed: dict | None = None   # what the browser was told to load
        self._given: set[tuple[int, str]] = set()
        self._cur: dict | None = None      # the series being worked on
        self._last_contact = time.monotonic()
        self._httpd: ThreadingHTTPServer | None = None
        self.skipped_other: list[str] = []
        db = sync._db(cfg)
        try:
            if scope:
                s = db.get_series(scope)
                if s is None:
                    raise SystemExit(f"no tracked series matching {scope!r}")
                rows = [s]
                if not providers.get(s["provider"]).browser_only:
                    raise SystemExit(f"{s['title']} is not read through the browser: "
                                     f"use `webnovel-audio sync --scope {s['slug']}`")
            else:
                rows = []
                for s in db.list_series():
                    if not s["enabled"]:
                        continue                       # paused: nothing at all
                    if providers.get(s["provider"]).browser_only:
                        rows.append(s)
                    else:
                        self.skipped_other.append(s["slug"])
            self._pending = [dict(id=s["id"], slug=s["slug"], url=s["url"],
                                  title=s["title"]) for s in rows]
        finally:
            db.close()
        self.series_ids = [s["id"] for s in self._pending]
        self.title = (self._pending[0]["title"] if len(self._pending) == 1
                      else f"{len(self._pending)} series")

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

    def seen(self, ext_version: str | None) -> None:
        """An authenticated request arrived, from an extension of this version."""
        self._contacted = True
        latest = extension_version()
        if (ext_version and latest and not self._hinted_old
                and version_tuple(ext_version) < version_tuple(latest)):
            self._hinted_old = True
            self.say(f"the browser extension is {ext_version}; {latest} is bundled: "
                     f"update it at http://127.0.0.1:{self.port}/extension")

    def _begin_series(self, db, s: dict) -> None:
        chs = db.chapters(s["id"])
        todo = db.outstanding(s["id"], "rendered", self.limit)
        self.emit({"event": "series", "slug": s["slug"], "title": s["title"],
                   "pending": len(todo),
                   "counts": {"rendered": sum(c["status"] == "rendered" for c in chs),
                              "errors": sum(c["status"] == "error" for c in chs),
                              "pending": len(db.pending(s["id"]))}})

    def next_action(self) -> dict:
        db = sync._db(self.cfg)
        try:
            with self._lock:
                self._last_contact = time.monotonic()
                while True:
                    if self.finished:
                        return {"action": "done", "reason": self.finished,
                                "say": self.finished[:1].upper() + self.finished[1:],
                                "clean": self.finished == "every series done",
                                "latest": extension_version()}
                    if self._handed:                # asked again before the page arrived
                        return self._fetch_action()
                    if self._cur is None:
                        if not self._pending:
                            if self._inflight:      # fetched everything; renders still going
                                why = ("finishing the last render"
                                       f"{'s' if self._inflight > 1 else ''}")
                                return {"action": "wait", "latest": extension_version(),
                                        "reason": why, "say": "Waiting: " + why}
                            self.finished = "every series done"
                            return {"action": "done", "reason": self.finished,
                                    "say": "Every series done", "clean": True,
                                    "latest": extension_version()}
                        self._cur = {**self._pending.pop(0), "phase": "index", "n": 0}
                        self._handed = {"url": self._cur["url"], "kind": "series",
                                        "number": 0, "title": self._cur["title"]}
                        continue
                    cur = self._cur
                    if self.limit and cur["n"] >= self.limit:
                        self._cur = None
                        continue
                    if self._inflight > self.ahead:
                        return {"action": "wait", "reason": "rendering",
                                "say": "Waiting: rendering", "latest": extension_version()}
                    row = next((r for r in db.con.execute(
                        "SELECT * FROM chapters WHERE series_id=? AND status='new' "
                        "ORDER BY ord", (cur["id"],))
                        if (cur["id"], r["source_id"]) not in self._given), None)
                    if row is None:
                        self._cur = None             # nothing outstanding: next series
                        continue
                    self._given.add((cur["id"], row["source_id"]))
                    self._handed = {"url": row["url"], "kind": "chapter",
                                    "number": row["ord"] + 1, "title": row["title"]}
        finally:
            db.close()

    def _fetch_action(self) -> dict:
        h = self._handed
        say = (f"Next series: {h['title']}" if h["kind"] == "series"
               else f"Next: #{h['number']} {h['title']}")
        return {"action": "fetch", **h, "say": say, "latest": extension_version()}

    def take_page(self, html: str) -> dict:
        db = sync._db(self.cfg)
        try:
            r = sync.ingest_raw(self.cfg, db, html)
            queue_these: list[tuple[int, int]] = []
            ok = r["result"] in ("ingested", "have")
            if ok and r.get("kind") == "series":
                s = db.get_series(r["series"])
                for c in db.chapters(s["id"]):       # work left from an earlier run
                    # `error` with its page already held is a render or parse that
                    # failed: tried again, once per run (`_queued`), in case it was
                    # ephemeral. An error with no page is left alone.
                    if c["status"] in ("fetched", "parsed") or (
                            c["status"] == "error" and c["raw_path"]
                            and os.path.exists(c["raw_path"]) and c["unlocked"]):
                        queue_these.append((s["id"], c["ord"] + 1))
                self._begin_series(db, s)
            elif ok and r.get("kind") == "chapter":
                s = db.get_series(r["series"])
                row = db.con.execute(
                    "SELECT status FROM chapters WHERE series_id=? AND ord=?",
                    (s["id"], r["number"] - 1)).fetchone()
                if row and row["status"] in ("fetched", "parsed"):
                    queue_these.append((s["id"], r["number"]))
        finally:
            db.close()
        self.say(f"{r['result']}: {r['detail']}")
        with self._lock:
            self._last_contact = time.monotonic()
            if ok:
                self._handed = None
                if r.get("kind") == "series" and self._cur:
                    self._cur["phase"] = "chapters"
                elif (r.get("kind") == "series" and self._cur is None and self._pending
                        and self._pending[0]["slug"] == r.get("series")):
                    # the page `grab` opened itself: it is this series' page already,
                    # so do not ask the browser to load it a second time
                    self._cur = {**self._pending.pop(0), "phase": "chapters", "n": 0}
                elif r.get("kind") == "chapter":
                    if r["result"] == "ingested":
                        self.ingested += 1
                        if self._cur:
                            self._cur["n"] += 1
                for key in queue_these:
                    if key not in self._queued:
                        self._queued.add(key)
                        self._inflight += 1
                        self._queue.put(key)
        if not ok:
            self.finish(f"the browser sent a page that was not usable: {r['detail']}")
        return {"ok": ok, **r}

    # -- rendering, on the calling thread ----------------------------------------
    def _render_emit(self, d: dict) -> None:
        """Each chapter is its own one-chapter run, which announces itself with a
        start, a series line and a done. This process says those once for the whole
        loop, so only the per-chapter events pass."""
        self._last_progress = time.monotonic()
        if d.get("event") not in ("start", "series", "done"):
            self.emit(d)

    def _render_chapter(self, series_id: int, number: int):
        db = sync._db(self.cfg)
        try:
            slug = db.series_by_id(series_id)["slug"]
        finally:
            db.close()
        return sync.run_stage(self.cfg, "rendered", slug, lo=number, hi=number,
                              backend=self.cfg.synth.backend, log=lambda *_: None,
                              emit=self._render_emit)

    def _real_preflight(self) -> None:
        from . import pipeline
        pipeline.preflight(self.cfg, self.cfg.synth.backend)

    def run(self, on_ready=None) -> int:
        """Serve and render until done. `on_ready(url)` is told the address to open."""
        try:
            self._preflight()
        except SystemExit as exc:                  # the environment: no point opening the browser
            self.say(f"cannot render: {exc}")
            self.emit({"event": "done", "stage": "rendered", "done": 0, "errors": 1, "skipped": 0})
            return 1
        if not self._pending:
            self.say("no series to fetch: " + (
                "none are enabled and read through the browser"))
            return 0
        self._httpd = _server(self)
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        self.emit({"event": "start", "stage": "rendered", "series": len(self._pending),
                   "dry_run": False, "range": None})
        self.say(f"{self.title}: fetching "
                 + (f"up to {self.limit} chapter(s) each" if self.limit else "everything outstanding")
                 + " through the browser")
        if self.skipped_other:
            self.say("not read through the browser (use sync): " + ", ".join(self.skipped_other))
        if on_ready:
            on_ready(f"{self._pending[0]['url']}#wa={self.port}:{self.token}")
        beat = started = time.monotonic()
        hinted = False
        try:
            while True:
                if not hinted and not self._contacted and time.monotonic() - started > self.hint_after:
                    hinted = True
                    self.say("the browser has not answered. Is the page-grabber extension "
                             f"installed? Install or update it at http://127.0.0.1:{self.port}/extension")
                try:
                    key = self._queue.get(timeout=0.5)
                except queue.Empty:
                    key = None
                if key is not None:
                    self._render_watched(*key)
                    continue
                if self._stalled or (self.finished and self._queue.empty()):
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
        self.say(f"finished: {self.finished}")
        self.emit({"event": "done", "stage": "rendered", "done": self.rendered,
                   "errors": self.errors, "skipped": 0})
        return 1 if self.errors else 0

    def _render_one(self, series_id: int, number: int) -> None:
        self._last_progress = time.monotonic()
        try:
            res = self._render(series_id, number)
            self.rendered += getattr(res, "rendered", 0) or 0
            self.errors += getattr(res, "errors", 0) or 0
        except (Exception, SystemExit) as exc:
            # A bad chapter is marked `error` inside the render and never gets here.
            # What does is the environment (or something unexpected): every further
            # chapter would fail the same way, so end the run and say why.
            self.errors += 1
            self.say(f"render of #{number} failed: {exc}")
            self.finish(f"render failed: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}")
        finally:
            self._last_progress = time.monotonic()
            with self._lock:
                self._inflight -= 1

    def _render_watched(self, series_id: int, number: int) -> None:
        """Render on a worker thread so a render that never returns can be noticed.
        The run gives up on it (`render stalled`) once nothing has begun or finished
        for `stall_timeout`; the worker is a daemon, abandoned at exit."""
        w = threading.Thread(target=self._render_one, args=(series_id, number), daemon=True)
        w.start()
        while w.is_alive():
            w.join(0.5)
            if time.monotonic() - self._last_progress > self.stall_timeout:
                self._stalled = True
                self.errors += 1
                self.say(f"render of #{number} made no progress for "
                         f"{int(self.stall_timeout // 60)} min: giving up on it")
                self.finish("render stalled")
                return


class PageCatcher:
    """Asks the browser for one page and hands it back: `series add` for a site that
    is read through the browser. The same server and protocol as GrabLoop."""

    def __init__(self, idle_timeout: float = 60.0):
        self.token = secrets.token_urlsafe(24)
        self.port = 0
        self.idle_timeout = idle_timeout
        self.page: str | None = None
        self.finished = ""
        self._lock = threading.Lock()

    def finish(self, reason: str) -> None:
        with self._lock:
            if not self.finished:
                self.finished = reason

    def say(self, message: str) -> None:
        pass

    def seen(self, ext_version: str | None) -> None:
        pass

    def next_action(self) -> dict:
        return {"action": "done", "reason": self.finished or "page received"}

    def take_page(self, html: str) -> dict:
        self.page = html
        self.finish("page received")
        return {"ok": True, "result": "received", "detail": "", "kind": "page"}

    def run(self, url: str, on_ready=None) -> str | None:
        """Serve until the page arrives or the browser stays silent. Returns it."""
        httpd = _server(self)
        self.port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            if on_ready:
                on_ready(f"{url}#wa={self.port}:{self.token}")
            deadline = time.monotonic() + self.idle_timeout
            while not self.finished and time.monotonic() < deadline:
                time.sleep(0.1)
            time.sleep(0.2)                        # let the browser read its reply
        finally:
            httpd.shutdown()
            httpd.server_close()
        return self.page


def open_in_browser(url: str) -> None:
    """The default browser, on this one address. Only ever on a user's request."""
    import subprocess
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def _server(loop) -> ThreadingHTTPServer:
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
            loop.seen(self.headers.get("X-Grab-Ext"))
            if theirs != str(PROTOCOL):
                older = theirs.isdigit() and int(theirs) < PROTOCOL or not theirs
                loop.say("the browser extension is " + ("out of date: update it at "
                         f"http://127.0.0.1:{port}/extension" if older else "newer than this "
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

        def _host_ok(self) -> bool:
            port = self.server.server_address[1]
            return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _bytes(self, body: bytes, ctype: str, name: str = "") -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if name:
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/extension", "/extension.zip"):    # public: no secrets in it
                if not self._host_ok():
                    return self._send(403, {"error": "refused (host)"})
                if self.path == "/extension":
                    return self._bytes(extension_page(), "text/html; charset=utf-8")
                return self._bytes(extension_zip(), "application/zip", "page-grabber.zip")
            if self._refuse("get"):
                return
            route, _, query = self.path.partition("?")
            if route == "/next":
                # long-poll: hold the request while the answer is "wait", so the
                # extension has nothing to sleep on or count
                try:
                    hold = min(MAX_HOLD, float(parse_qs(query).get("wait", ["0"])[0]))
                except ValueError:
                    hold = 0.0
                deadline = time.monotonic() + hold
                while True:
                    a = loop.next_action()
                    if a.get("action") != "wait" or time.monotonic() >= deadline:
                        break
                    time.sleep(0.25)
                self._send(200, a)
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
