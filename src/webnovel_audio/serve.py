"""A tiny LAN server: the web player, podcast feeds and audio files for the library.

Open http://<this-machine>:<port>/ on any device: the player is the home page (browse,
play, resume, see what's new). Each series page has its podcast feed for apps such as
AntennaPod. New chapters appear everywhere as `sync` renders them.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from . import bundle, player
from .db import DB
from .feed import audio_mime, build_feed
from .safepath import safe_slug as _safe_slug

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
_WEB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WEB_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
              ".mjs": "text/javascript; charset=utf-8", ".js": "text/javascript; charset=utf-8",
              ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
              ".webmanifest": "application/manifest+json"}
_MAX_BODY = 4096
#: how long a request waits for the renderer's write lock before answering 503.
#: Reads never wait (WAL); only position saves can, and the page just retries.
DB_BUSY_MS = 2000
_BAD = object()          # _read_json's "I already sent the error response"

_HOST_RE = re.compile(r"[^A-Za-z0-9.\-:\[\]]")

#: never redirected: the owner's own machine, whatever the feed's canonical host
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _host_port(value: str) -> tuple[str, int] | None:
    """(hostname, port) from a Host header or a URL's netloc; None if unusable.
    A missing port is 80, so `host` and `host:80` compare equal."""
    try:
        parts = urllib.parse.urlsplit("//" + value.strip())
        if not parts.hostname:
            return None
        return parts.hostname.lower(), parts.port or 80
    except ValueError:
        return None


# Phones/browsers open speculative + HTTPS-probe connections and drop them; these
# surface here as noisy but harmless socket errors.
_QUIET_ERRORS = (ConnectionError, BrokenPipeError, TimeoutError, ssl.SSLError)

def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


#: audio and cover fetches arrive as a burst of range requests per play; log
#: one per client and file per this many seconds so the log stays small
_ACCESS_DEDUP_S = 600
_ACCESS_LOG_BYTES, _ACCESS_LOG_KEEP = 1_000_000, 3
_UNPRINTABLE_RE = re.compile(r"[^\x20-\x7e]")


def _access_logger(path: str) -> logging.Logger | None:
    """A size-capped rotating log (about 4 MB in total), or None if `path` is empty.

    A private Logger rather than `logging.getLogger`, so repeated `serve()`
    calls (and tests) never stack handlers on a shared one.
    """
    if not path:
        return None
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=_ACCESS_LOG_BYTES, backupCount=_ACCESS_LOG_KEEP, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%dT%H:%M:%S"))
    lg = logging.Logger("webnovel_audio.access", logging.INFO)
    lg.addHandler(handler)
    return lg


def _clean(value, limit: int) -> str:
    """Client-controlled text made safe for a one-line log entry."""
    return _UNPRINTABLE_RE.sub("?", str(value or "-"))[:limit]


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    access: logging.Logger | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.recent: dict[tuple[str, str], float] = {}     # (client, path) -> last logged

    def handle_error(self, request, client_address):
        import sys

        if not isinstance(sys.exc_info()[1], _QUIET_ERRORS):
            super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    server_version = "webnovel-audio"

    def handle(self):
        try:
            super().handle()
        except _QUIET_ERRORS:
            pass

    # -- helpers --------------------------------------------------------
    @property
    def cfg(self) -> Config:
        return self.server.cfg  # type: ignore[attr-defined]

    @property
    def library(self) -> str:
        return os.path.expanduser(self.cfg.library.library_dir)

    def _base_url(self) -> str:
        if self.cfg.serve.base_url:
            return self.cfg.serve.base_url.rstrip("/")
        # the Host header is client-controlled: keep only valid host/port chars
        host = _HOST_RE.sub("", self.headers.get("Host") or "")
        if not host:
            host = f"{_lan_ip()}:{self.server.server_address[1]}"
        return f"http://{host}"

    def _feed_redirect(self, dslug: str) -> bool:
        """301 a feed request that arrived on a host other than the canonical one.

        AntennaPod (and most podcast apps) rewrite a subscription's stored URL
        when its feed answers with a permanent redirect; they cannot be edited
        by hand. Only feeds redirect: `/audio`, `/cover` and the index are left
        alone, and the target is built from config + the validated slug, never
        from the request, so there is no open redirect.
        """
        target = self.cfg.serve.tailnet_url.strip().rstrip("/") \
            if self.cfg.serve.redirect_to_tailnet else ""
        want = _host_port(urllib.parse.urlsplit(target).netloc) if target else None
        got = _host_port(self.headers.get("Host") or "")
        if want is None or got is None or got[0] in _LOCAL_HOSTS or got == want:
            return False
        self.send_response(301)
        self.send_header("Location", f"{target}/feed/{dslug}.xml")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    # -- access log: one line per response, written when its headers are done ----
    def send_response(self, code, message=None):
        self._code, self._clen, self._loc = int(code), "", ""
        super().send_response(code, message)

    def send_header(self, keyword, value):
        low = keyword.lower()
        if low == "content-length":
            self._clen = str(value)
        elif low == "location":
            self._loc = str(value)
        super().send_header(keyword, value)

    def end_headers(self):
        super().end_headers()
        self._access()

    def _access(self) -> None:
        lg = self.server.access  # type: ignore[attr-defined]
        if lg is None:
            return
        path = self.path.split("?", 1)[0]
        client = self.client_address[0]
        if path.startswith(("/audio/", "/cover/", "/player/", "/api/")):
            recent, now = self.server.recent, time.monotonic()  # type: ignore[attr-defined]
            if now - recent.get((client, path), -_ACCESS_DEDUP_S) < _ACCESS_DEDUP_S:
                return
            if len(recent) > 500:
                recent.clear()
            recent[(client, path)] = now
        line = (f"{client} {self.command} {_clean(path, 160)} {self._code}"
                + (f" {self._clen}B" if self._clen and self.command != "HEAD" else "")
                + (f" -> {_clean(self._loc, 200)}" if self._loc else "")
                + f" host={_clean(self.headers.get('Host'), 80)}"
                + f' ua="{_clean(self.headers.get("User-Agent"), 60)}"')
        lg.info(line)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args):  # quieter
        pass

    # -- routes --------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.unquote(self.path.split("?", 1)[0])
        try:
            if "\x00" in path:
                return self._send(400, b"bad request\n", "text/plain")
            if path in ("/", "/index.html", "/player", "/player/"):
                return self._static("player.html")
            if path.startswith("/player/"):
                return self._static(path[len("/player/"):])
            if path.startswith("/api/"):
                return self._api_get(path)
            if path.startswith("/feed/") and path.endswith(".xml"):
                return self._feed(_safe_slug(path[len("/feed/"):-len(".xml")], ""))
            if path.startswith("/audio/"):
                parts = path[len("/audio/"):].split("/")
                slug = _safe_slug(parts[0], "") if parts else ""
                bdir = self._bundle(slug) if slug and len(parts) > 1 else None
                if not bdir:
                    return self._send(404, b"not found\n", "text/plain")
                return self._file(bundle.artifact(bdir, "chapters", parts[-1]),
                                  root=bdir, fallback_mime="audio/ogg")
            if path.startswith("/cover/"):
                slug = _safe_slug(path[len("/cover/"):].removesuffix(".jpg"), "")
                bdir = self._bundle(slug) if slug else None
                if not bdir:
                    return self._send(404, b"not found\n", "text/plain")
                return self._file(bundle.artifact(bdir, "covers", "cover.jpg"),
                                  root=bdir, fallback_mime="image/jpeg")
            self._send(404, b"not found\n", "text/plain")
        except sqlite3.OperationalError:
            self._busy()
        except (ValueError, OSError, *_QUIET_ERRORS):
            self._send(404, b"not found\n", "text/plain")

    def do_POST(self):
        self._api_write(urllib.parse.unquote(self.path.split("?", 1)[0]))

    do_PUT = do_POST

    # -- web player: static files and the JSON api --------------------------------
    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _static(self, name: str) -> None:
        ext = os.path.splitext(name)[1].lower()
        path = os.path.join(WEB_DIR, name)
        if not _WEB_NAME_RE.match(name) or ext not in _WEB_TYPES or not os.path.isfile(path):
            return self._send(404, b"not found\n", "text/plain")
        with open(path, "rb") as fh:
            self._send(200, fh.read(), _WEB_TYPES[ext], {"Cache-Control": "no-cache"})

    def _profile_in(self, db: DB, raw: str) -> str | None:
        name = player.valid_profile_name(raw)
        return name if name and name in db.list_profiles() else None

    def _api_get(self, path: str) -> None:
        parts = [p for p in path.split("/") if p][1:]               # drop "api"
        db = self._db()
        try:
            if parts == ["library"]:
                return self._json(200, player.library(self.cfg, db))
            if parts == ["profiles"]:
                return self._json(200, {"profiles": db.list_profiles()})
            if len(parts) == 2 and parts[0] == "positions":
                profile = self._profile_in(db, parts[1])
                if not profile:
                    return self._json(404, {"error": "unknown profile"})
                return self._json(200, player.positions(db, profile))
            self._json(404, {"error": "not found"})
        finally:
            db.close()

    def _read_json(self):
        """The request's JSON body, or `_BAD` after sending the error response.

        Only `application/json` is accepted, and no CORS headers are ever sent, so
        a web page on some other site cannot make a visitor's browser write here
        (that would need a preflight this server never approves).
        """
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            self._json(415, {"error": "send application/json"})
            return _BAD
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 < n <= _MAX_BODY:
            self._json(413 if n > _MAX_BODY else 400, {"error": "bad body size"})
            return _BAD
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            self._json(400, {"error": "bad json"})
            return _BAD

    def _api_write(self, path: str) -> None:
        try:
            parts = [p for p in path.split("/") if p]
            if parts[:1] != ["api"]:
                return self._json(404, {"error": "not found"})
            body = self._read_json()
            if body is _BAD:
                return
            db = self._db()
            try:
                if self.command == "POST" and parts == ["api", "profiles"]:
                    name = player.valid_profile_name(body.get("name") if isinstance(body, dict) else "")
                    if not name:
                        return self._json(400, {"error": "use 1-24 letters, digits, spaces . _ ' -"})
                    return self._json(200, {"profile": db.add_profile(name)})
                if self.command == "PUT" and len(parts) == 4 and parts[1] == "positions":
                    profile = self._profile_in(db, parts[2])
                    if not profile:
                        return self._json(404, {"error": "unknown profile"})
                    saved = player.save_position(db, profile, _safe_slug(parts[3], ""), body)
                    if saved is None:
                        return self._json(400, {"error": "unknown series or chapter"})
                    return self._json(200, saved)
                self._json(404, {"error": "not found"})
            finally:
                db.close()
        except sqlite3.OperationalError:       # "database is locked": the renderer has it
            self._busy()
        except (*_QUIET_ERRORS, OSError):
            pass

    def _db(self) -> DB:
        # no schema pass per request: `serve()` initialised the DB once, and a
        # request that rewrote nothing must never take the write lock
        return DB(self.cfg.library.state_db, init=False, busy_ms=DB_BUSY_MS)

    def _busy(self) -> None:
        self._send(503, b'{"error": "busy, try again"}', "application/json; charset=utf-8",
                   {"Retry-After": "2", "Cache-Control": "no-store"})

    def _bundle(self, slug: str) -> str | None:
        """The series' bundle directory, or None if it isn't tracked. Files are
        served from here rather than from a fixed library root, so a relocated
        bundle still serves."""
        db = self._db()
        try:
            s = db.get_series(slug)
            return bundle.bundle_dir(self.cfg, s) if s else None
        finally:
            db.close()

    def _feed(self, slug: str):
        if not slug:
            return self._send(404, b"unknown series\n", "text/plain")
        db = self._db()
        s = db.get_series(slug)
        if not s:
            db.close()
            return self._send(404, b"unknown series\n", "text/plain")
        dslug = _safe_slug(s["slug"], slug)
        if self._feed_redirect(dslug):
            db.close()
            return
        base = self._base_url()
        cover_local = os.path.exists(bundle.artifact(
            bundle.bundle_dir(self.cfg, s), "covers", "cover.jpg"))
        xml = build_feed(s, db.chapters(s["id"]), base,
                             volumes=db.volume_map(s["id"]),
                         self_url=f"{base}/feed/{dslug}.xml", cover_local=cover_local)
        db.close()
        self._send(200, xml.encode(), "application/rss+xml; charset=utf-8")

    def _file(self, path: str, *, fallback_mime: str, root: str | None = None):
        # confined to the bundle being served, not to one fixed library root —
        # a relocated series lives outside `library_dir` and must still serve
        root = os.path.realpath(root or self.library)
        path = os.path.realpath(path)
        if os.path.commonpath((root, path)) != root or not os.path.isfile(path):
            return self._send(404, b"not found\n", "text/plain")
        size = os.path.getsize(path)
        ctype = audio_mime(path) if fallback_mime.startswith("audio") else fallback_mime
        rng = self.headers.get("Range")
        with open(path, "rb") as fh:
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else 0
                end = int(b) if b else size - 1
                end = min(end, size - 1)
                fh.seek(start)
                data = fh.read(end - start + 1)
                self._send(206, data, ctype, {
                    "Content-Range": f"bytes {start}-{end}/{size}",
                    "Accept-Ranges": "bytes",
                })
            else:
                self._send(200, fh.read(), ctype, {"Accept-Ranges": "bytes"})


def _print_qr(url: str, log) -> None:
    for cmd in (["qr", "-m", "2", url],
                ["qrencode", "-t", "ANSIUTF8", "-m", "2", "--", url]):
        if shutil.which(cmd[0]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if out.returncode == 0 and out.stdout.strip():
                    log(out.stdout)
                    return
            except (OSError, subprocess.SubprocessError):
                pass
    log("(install 'qrencode' for a scannable QR of this URL)")


def _banner(cfg: Config, port: int, log) -> None:
    """Where the server can be reached, with a scannable QR for each address."""
    local = cfg.serve.base_url or f"http://{_lan_ip()}:{port}"
    tailnet = cfg.serve.tailnet_url.strip().rstrip("/")
    log(f"serving library on:   (Ctrl-C to stop)\n  local      {local}"
        + (f"\n  tailscale  {tailnet}" if tailnet else ""))
    if tailnet and cfg.serve.redirect_to_tailnet:
        log("! redirect_to_tailnet is ON: every feed requested on another host "
            f"is redirected (301) to {tailnet}. Turn it off once the phone has refreshed.")
    if cfg.serve.access_log:
        log(f"access log: {os.path.expanduser(cfg.serve.access_log)} "
            f"(rotates at {_ACCESS_LOG_BYTES // 1_000_000} MB, {_ACCESS_LOG_KEEP} kept)")
    if cfg.serve.qr:
        for label, url in (("local", local), ("tailscale", tailnet)):
            if url:
                log(f"scan for {label}:")
                _print_qr(url, log)


def serve(cfg: Config, host: str | None = None, port: int | None = None, log=print) -> None:
    host = host or cfg.serve.host
    port = int(port or cfg.serve.port)
    httpd = _Server((host, port), _Handler)
    httpd.cfg = cfg  # type: ignore[attr-defined]
    DB(cfg.library.state_db).close()           # create/migrate once; requests then skip it
    httpd.access = _access_logger(cfg.serve.access_log)
    _banner(cfg, port, log)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("\nstopped.")
    finally:
        httpd.server_close()
