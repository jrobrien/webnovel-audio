"""A tiny LAN server: podcast feeds + audio files for the library.

Point a podcast app on your phone at  http://<this-machine>:<port>/  and it lists
every tracked series with a one-tap feed URL to subscribe. New chapters appear in
the feed as `sync` renders them.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import ssl
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

from .config import Config
from .db import DB
from .feed import audio_mime, build_feed
from .royalroad import _safe_slug

_HOST_RE = re.compile(r"[^A-Za-z0-9.\-:\[\]]")


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


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

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
        return os.path.expanduser(self.cfg.royalroad.library_dir)

    def _base_url(self) -> str:
        if self.cfg.serve.base_url:
            return self.cfg.serve.base_url.rstrip("/")
        # the Host header is client-controlled: keep only valid host/port chars
        host = _HOST_RE.sub("", self.headers.get("Host") or "")
        if not host:
            host = f"{_lan_ip()}:{self.server.server_address[1]}"
        return f"http://{host}"

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
                return self._file(os.path.join(self.library, *path[len("/audio/"):].split("/")),
                                  fallback_mime="audio/ogg")
            if path.startswith("/cover/"):
                slug = _safe_slug(path[len("/cover/"):].removesuffix(".jpg"), "")
                if not slug:
                    return self._send(404, b"not found\n", "text/plain")
                return self._file(os.path.join(self.library, slug, "cover.jpg"),
                                  fallback_mime="image/jpeg")
            self._send(404, b"not found\n", "text/plain")
        except (ValueError, OSError, *_QUIET_ERRORS):
            self._send(404, b"not found\n", "text/plain")

    def _db(self) -> DB:
        return DB(self.cfg.royalroad.state_db)

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
            if os.path.exists(os.path.join(self.library, slug, "cover.jpg")):
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
        base = self._base_url()
        dslug = _safe_slug(s["slug"], slug)
        cover_local = os.path.exists(os.path.join(self.library, dslug, "cover.jpg"))
        xml = build_feed(s, db.chapters(s["id"]), base,
                             volumes=db.volume_map(s["id"]),
                         self_url=f"{base}/feed/{dslug}.xml", cover_local=cover_local)
        db.close()
        self._send(200, xml.encode(), "application/rss+xml; charset=utf-8")

    def _file(self, path: str, *, fallback_mime: str):
        root = os.path.realpath(self.library)
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


def serve(cfg: Config, host: str | None = None, port: int | None = None, log=print) -> None:
    host = host or cfg.serve.host
    port = int(port or cfg.serve.port)
    httpd = _Server((host, port), _Handler)
    httpd.cfg = cfg  # type: ignore[attr-defined]
    shown = cfg.serve.base_url or f"http://{_lan_ip()}:{port}"
    log(f"serving library on {shown}   (Ctrl-C to stop)")
    log("open that on your phone, or scan:")
    if cfg.serve.qr:
        _print_qr(shown, log)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("\nstopped.")
    finally:
        httpd.server_close()
