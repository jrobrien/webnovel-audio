import hashlib
import re
import sqlite3
import time

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from webnovel_audio.providers import browser_cookies as bc
from webnovel_audio.providers.http import PoliteClient

_EPOCH = 11644473600


def _enc(value: str, host: str, password: bytes, tag: bytes) -> bytes:
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1, 16)
    pad = padding.PKCS7(128).padder()
    data = pad.update(hashlib.sha256(host.encode()).digest() + value.encode()) + pad.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return tag + enc.update(data) + enc.finalize()


def _jar(tmp_path, monkeypatch, rows):
    prof = tmp_path / "google-chrome" / "Default" / "Network"
    prof.mkdir(parents=True)
    db = sqlite3.connect(prof / "Cookies")
    db.execute("CREATE TABLE cookies (host_key, name, value, encrypted_value, expires_utc)")
    db.executemany("INSERT INTO cookies VALUES (?,?,?,?,?)", rows)
    db.commit()
    db.close()
    monkeypatch.setitem(bc.BROWSERS, "chrome",
                        (str(tmp_path / "google-chrome"), "chrome", "chrome-x"))
    monkeypatch.setattr(bc, "_keyring_password", lambda b: b"keyring-pw")


def _later(days):
    return int((time.time() + _EPOCH + days * 86400) * 1e6)


def test_reads_only_unexpired_clearance_cookies_of_the_domain(tmp_path, monkeypatch):
    h = "www.royalroad.com"
    _jar(tmp_path, monkeypatch, [
        (h, "techaro.lol-anubis-auth-x", "", _enc("jwt", h, b"keyring-pw", b"v11"), _later(5)),
        (".royalroad.com", "cf_clearance", "", _enc("cf", ".royalroad.com", b"peanuts", b"v10"), _later(5)),
        (h, "techaro.lol-anubis-old", "", _enc("stale", h, b"keyring-pw", b"v11"), _later(-1)),
        (h, ".AspNetCore.Identity.Application", "", _enc("login", h, b"keyring-pw", b"v11"), _later(5)),
        ("example.com", "cf_clearance", "x", b"", _later(5)),
        ("notroyalroad.com", "cf_clearance", "x", b"", _later(5)),
    ])
    got = bc.read_cookies("chrome", "royalroad.com", only=bc.CLEARANCE)
    assert got == {"techaro.lol-anubis-auth-x": "jwt", "cf_clearance": "cf"}
    everything = bc.read_cookies("chrome", "royalroad.com")
    assert everything[".AspNetCore.Identity.Application"] == "login"
    assert "techaro.lol-anubis-old" not in everything


def test_missing_profile_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setitem(bc.BROWSERS, "chrome", (str(tmp_path), "chrome", "chrome-x"))
    with pytest.raises(bc.BrowserCookieError, match="no cookie file"):
        bc.read_cookies("chrome", "royalroad.com")


def test_anubis_page_is_reported_not_parsed():
    page = '<title>Making sure</title><script id="anubis_challenge" type="application/json">{}</script>'
    c = PoliteClient(delay=0, label="royalroad")
    c._c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page)))
    with pytest.raises(SystemExit, match=re.compile("Anubis.*login --from-browser", re.S)):
        c.get("https://www.royalroad.com/fiction/1")
