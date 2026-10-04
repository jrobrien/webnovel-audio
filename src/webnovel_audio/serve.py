"""A tiny LAN server: podcast feeds + audio files for the library.

Point a podcast app on your phone at  http://<this-machine>:<port>/  and it lists
every tracked series with a one-tap feed URL to subscribe. New chapters appear in
the feed as `sync` renders them.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import shutil
import socket
import ssl
import subprocess
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

from .config import Config
from . import bundle
from .db import DB
from .feed import audio_mime, build_feed
from .safepath import safe_slug as _safe_slug

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


def _h(value) -> str:
    """HTML-escape for both text and quoted-attribute contexts."""
    return escape(str(value), {'"': "&quot;", "'": "&#39;"})

# Phones/browsers open speculative + HTTPS-probe connections and drop them; these
# surface here as noisy but harmless socket errors.
_QUIET_ERRORS = (ConnectionError, BrokenPipeError, TimeoutError, ssl.SSLError)

_INDEX_CSS = """
:root{--bg:#fafaf9;--card:#fff;--fg:#1c1917;--dim:#78716c;--line:#e7e5e4;
  --accent:#4f46e5;--on-accent:#fff;--code:#f5f5f4;--ok:#059669}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--card:#202024;--fg:#e7e5e4;
  --dim:#a1a1aa;--line:#33333a;--accent:#818cf8;--on-accent:#16161a;--code:#27272b;--ok:#34d399}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:44rem;margin:0 auto;padding:2rem 1.1rem 3rem}
h1{font-size:1.4rem;margin:0 0 .3rem}
.lead{color:var(--dim);margin:.2rem 0 1.6rem}
.lead code,.empty code{background:var(--code);padding:.1em .35em;border-radius:5px;font-size:.9em}
.grid{display:flex;flex-direction:column;gap:1rem}
.card{display:flex;align-items:flex-start;gap:1rem;background:var(--card);
  border:1px solid var(--line);border-radius:14px;padding:1rem;
  box-shadow:0 1px 2px #0000000d}
.cover{flex:none;align-self:flex-start;width:88px;aspect-ratio:2/3;object-fit:cover;
  border-radius:8px;background:var(--code)}
.cover.ph{display:flex;align-items:center;justify-content:center;font-size:2rem;
  font-weight:700;color:var(--dim)}
.body{min-width:0;display:flex;flex-direction:column;gap:.35rem;flex:1}
.body h2{font-size:1.05rem;margin:0;line-height:1.3}
.by{color:var(--dim);font-size:.85rem;margin-top:-.2rem}
.meta{color:var(--dim);font-size:.85rem}
.dim{opacity:.7}
.url{display:block;font:.8rem/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
  background:var(--code);border:1px solid var(--line);border-radius:8px;
  padding:.45rem .55rem;word-break:break-all;user-select:all;cursor:pointer;
  margin:.15rem 0 .1rem}
.url:focus{outline:2px solid var(--accent);outline-offset:1px}
.url[data-copied]::after{content:" — copied";color:var(--ok);user-select:none}
.actions{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:.35rem}
.btn{text-decoration:none;font-size:.82rem;font-weight:600;padding:.4rem .75rem;
  border-radius:8px;background:var(--accent);color:var(--on-accent);
  border:1px solid transparent;white-space:nowrap}
.btn.ghost{background:transparent;color:var(--fg);border-color:var(--line)}
.btn:active{transform:translateY(1px)}
.empty{color:var(--dim)}
""".strip()

_INDEX_JS = """
for(const el of document.querySelectorAll('.url')){
 el.addEventListener('click',()=>{
  const r=document.createRange();r.selectNodeContents(el);
  const s=getSelection();s.removeAllRanges();s.addRange(r);
  if(navigator.clipboard)navigator.clipboard.writeText(el.textContent).then(()=>{
   el.setAttribute('data-copied','');setTimeout(()=>el.removeAttribute('data-copied'),1400);
  }).catch(()=>{});
 });
}
""".strip()


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
        if path.startswith(("/audio/", "/cover/")):
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
            if path in ("/", "/index.html"):
                return self._index()
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
        except (ValueError, OSError, *_QUIET_ERRORS):
            self._send(404, b"not found\n", "text/plain")

    def _db(self) -> DB:
        return DB(self.cfg.library.state_db)

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

    def _index(self):
        db = self._db()
        base = self._base_url()
        cards = []
        for s in db.list_series():
            chs = db.chapters(s["id"])
            done = sum(1 for c in chs if c["status"] == "rendered")
            pend = sum(1 for c in chs if c["status"] in ("new", "fetched",
                                                         "parsed", "error"))
            slug = _safe_slug(s["slug"], "")
            if not slug:
                continue
            feed = f"{base}/feed/{slug}.xml"
            tail = feed.split("://", 1)[1]
            if os.path.exists(bundle.artifact(
                    bundle.bundle_dir(self.cfg, s), "covers", "cover.jpg")):
                art = f'<img class=cover src="{_h(base)}/cover/{slug}.jpg" alt="" loading=lazy>'
            else:
                art = f'<div class="cover ph">{_h((s["title"] or "?")[:1].upper())}</div>'
            meta = f"{done} episode{'s' * (done != 1)}"
            if pend:
                meta += f' <span class=dim>· {pend} pending</span>'
            author = s["author"] or ""
            cards.append(
                f'<article class=card>{art}<div class=body>'
                f"<h2>{_h(s['title'])}</h2>"
                + (f"<div class=by>{_h(author)}</div>" if author else "")
                + f"<div class=meta>{meta}</div>"
                f'<code class=url tabindex=0>{_h(feed)}</code>'
                f'<div class=actions><a class=btn href="pcast://{_h(tail)}">Add to AntennaPod</a>'
                f'<a class="btn ghost" href="{_h(feed)}">View feed</a></div>'
                "</div></article>"
            )
        db.close()

        body = (
            "".join(cards) if cards else
            "<p class=empty>No series yet. On the server:<br>"
            "<code>webnovel-audio series add &lt;fiction-url&gt;</code> then "
            "<code>webnovel-audio sync</code>.</p>"
        )
        html = (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>webnovel-audio</title>"
            f"<style>{_INDEX_CSS}</style>"
            "<main><h1>webnovel-audio</h1>"
            "<p class=lead>Paste a feed's <code>http://</code> URL into your podcast app's "
            "<b>Add by RSS / URL</b> field — AntennaPod, Podcast Addict or gPodder "
            "(they play Opus; Apple Podcasts and Overcast don't).</p>"
            f"<div class=grid>{body}</div></main>"
            f"<script>{_INDEX_JS}</script></html>"
        )
        self._send(200, html.encode(), "text/html; charset=utf-8")

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
    httpd.access = _access_logger(cfg.serve.access_log)
    _banner(cfg, port, log)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("\nstopped.")
    finally:
        httpd.server_close()
