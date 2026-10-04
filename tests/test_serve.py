import http.client
import threading

import pytest

from webnovel_audio.config import Config
from webnovel_audio.db import DB
from webnovel_audio.providers import SeriesInfo
from webnovel_audio.serve import _Handler, _Server, _access_logger, _host_port

CANON = "http://box.example.ts.net:8080"


@pytest.fixture
def server(tmp_path):
    started = []

    def start(tailnet="", redirect=None, access_log=None):
        """`tailnet` is the tailnet URL; it switches the redirect on unless `redirect` says otherwise."""
        cfg = Config()
        cfg.library.state_db = str(tmp_path / "s.db")
        cfg.library.library_dir = str(tmp_path / "lib")
        cfg.serve.tailnet_url = tailnet
        cfg.serve.redirect_to_tailnet = bool(tailnet) if redirect is None else redirect
        db = DB(cfg.library.state_db)
        db.upsert_series(SeriesInfo(provider="royalroad", source_id="9", slug="demo",
                                    title="Demo", url="https://rr/9"))
        db.close()
        httpd = _Server(("127.0.0.1", 0), _Handler)
        httpd.cfg = cfg
        httpd.access = _access_logger(str(access_log)) if access_log else None
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return httpd.server_address[1]

    yield start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


def get(port, path, host=None, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    if host is None:
        conn.putrequest(method, path, skip_host=True)
        conn.endheaders()
    else:
        conn.request(method, path, headers={"Host": host})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def test_host_port_parsing():
    assert _host_port("Box.example.ts.net:8080") == ("box.example.ts.net", 8080)
    assert _host_port("example.com") == ("example.com", 80)
    assert _host_port("[::1]:8080") == ("::1", 8080)
    assert _host_port("") is None and _host_port("host:notaport") is None


def test_no_redirect_configured_serves_every_host_as_before(server):
    port = server()
    resp, body = get(port, "/feed/demo.xml", host="192.168.1.50:8080")
    assert resp.status == 200 and b"http://192.168.1.50:8080/feed/demo.xml" in body


def test_feed_on_another_host_gets_a_permanent_redirect(server):
    port = server(CANON)
    resp, body = get(port, "/feed/demo.xml", host="192.168.1.50:8080")
    assert resp.status == 301 and body == b""
    assert resp.getheader("Location") == f"{CANON}/feed/demo.xml"
    # a HEAD request is redirected the same way
    resp, _ = get(port, "/feed/demo.xml", host="192.168.1.50:8080", method="HEAD")
    assert resp.status == 301 and resp.getheader("Location") == f"{CANON}/feed/demo.xml"


def test_canonical_host_serves_the_feed_with_its_own_urls(server):
    port = server(CANON)
    for host in ("box.example.ts.net:8080", "BOX.example.ts.net:8080"):
        resp, body = get(port, "/feed/demo.xml", host=host)
        assert resp.status == 200
        # the feed carries the host it was asked on (casing and all)
        assert f'href="{CANON}/feed/demo.xml"'.encode() in body.lower()


def test_local_use_is_never_redirected(server):
    port = server(CANON)
    for host in (f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"):
        resp, _ = get(port, "/feed/demo.xml", host=host)
        assert resp.status == 200, host


def test_only_feeds_redirect(server):
    port = server(CANON)
    other = "192.168.1.50:8080"
    assert get(port, "/", host=other)[0].status == 200
    assert get(port, "/audio/demo/001-x.opus", host=other)[0].status == 404
    assert get(port, "/cover/demo.jpg", host=other)[0].status == 404
    # an unknown series is a 404, not a redirect to somewhere that would also 404
    assert get(port, "/feed/nope.xml", host=other)[0].status == 404


def test_redirect_target_never_comes_from_the_request(server):
    port = server(CANON)
    resp, _ = get(port, "/feed/demo.xml", host="evil.example:80")
    assert resp.status == 301 and resp.getheader("Location") == f"{CANON}/feed/demo.xml"
    resp, _ = get(port, "/feed/demo.xml?next=http://evil.example/", host="192.168.1.50:8080")
    assert resp.status == 301 and resp.getheader("Location") == f"{CANON}/feed/demo.xml"


def test_a_request_without_a_host_header_is_served(server):
    port = server(CANON)
    assert get(port, "/feed/demo.xml", host=None)[0].status == 200


def test_tailnet_url_alone_does_not_redirect(server):
    # the address is configured (second QR) but the migration switch is off
    port = server(CANON, redirect=False)
    resp, _ = get(port, "/feed/demo.xml", host="192.168.1.50:8080")
    assert resp.status == 200


def test_banner_prints_both_addresses_and_a_qr_each(monkeypatch):
    import webnovel_audio.serve as srv

    monkeypatch.setattr(srv, "_lan_ip", lambda: "192.168.1.50")
    qrs = []
    monkeypatch.setattr(srv, "_print_qr", lambda url, log: qrs.append(url))
    cfg = Config()
    cfg.serve.tailnet_url = CANON + "/"
    out = []
    srv._banner(cfg, 8080, out.append)
    text = "\n".join(out)
    assert "http://192.168.1.50:8080" in text and CANON in text
    assert qrs == ["http://192.168.1.50:8080", CANON]
    assert "redirect_to_tailnet is ON" not in text

    cfg.serve.redirect_to_tailnet = True
    out.clear()
    srv._banner(cfg, 8080, out.append)
    assert "redirect_to_tailnet is ON" in "\n".join(out)

    # without a tailnet address: one address, one QR, as before
    qrs.clear()
    srv._banner(Config(), 8080, lambda *_: None)
    assert qrs == ["http://192.168.1.50:8080"]


def _log_lines(path):
    return path.read_text().splitlines() if path.exists() else []


def test_access_log_records_feeds_and_the_redirect_target(server, tmp_path):
    log = tmp_path / "logs" / "access.log"
    port = server(CANON, access_log=log)
    get(port, "/feed/demo.xml", host="192.168.1.50:8080")            # redirected
    get(port, "/feed/demo.xml", host="box.example.ts.net:8080")      # served
    get(port, "/", host="192.168.1.50:8080")
    get(port, "/feed/nope.xml", host="192.168.1.50:8080")           # unknown
    lines = _log_lines(log)
    assert len(lines) == 4
    assert "127.0.0.1 GET /feed/demo.xml 301" in lines[0]
    assert f"-> {CANON}/feed/demo.xml host=192.168.1.50:8080" in lines[0]
    assert "GET /feed/demo.xml 200" in lines[1] and "B host=box.example.ts.net:8080" in lines[1]
    assert "GET / 200" in lines[2] and "GET /feed/nope.xml 404" in lines[3]
    assert lines[0][:4].isdigit()                                    # timestamped


def test_access_log_collapses_repeated_audio_fetches(server, tmp_path):
    log = tmp_path / "access.log"
    port = server(access_log=log)
    for _ in range(5):                                               # a play's range requests
        get(port, "/audio/demo/001-x.opus", host="a:8080")
    get(port, "/audio/demo/002-y.opus", host="a:8080")               # a different file
    get(port, "/cover/demo.jpg", host="a:8080")
    assert len(_log_lines(log)) == 3


def test_access_log_cleans_client_controlled_text(server, tmp_path):
    log = tmp_path / "access.log"
    port = server(access_log=log)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/feed/demo.xml",
                 headers={"Host": "h:80", "User-Agent": "bad\x01agent " + "x" * 200})
    conn.getresponse().read()
    conn.close()
    (line,) = _log_lines(log)
    assert "\x01" not in line and "bad?agent" in line and len(line) < 300


def test_access_log_is_off_when_unset_and_capped_when_on(server, tmp_path):
    log = tmp_path / "access.log"
    port = server()                                                  # no logger
    get(port, "/feed/demo.xml", host="a:8080")
    assert not log.exists()

    lg = _access_logger(str(log))
    (handler,) = lg.handlers
    assert handler.maxBytes == 1_000_000 and handler.backupCount == 3
    assert _access_logger("") is None
