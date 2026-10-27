"""Read a site's cookies out of a Chromium-family browser's cookie jar.

For the bot check (Anubis, Cloudflare) a site puts in front of its pages: the
browser solves it, and the clearance cookie it earns is what we replay. Linux
only. Cookie values are AES-128-CBC encrypted under a key derived from the
"<Browser> Safe Storage" secret in the desktop keyring (libsecret); a profile
with no keyring entry uses the fixed password "peanuts" instead.

The browser can be running: its cookie file is copied before it is opened, so
nothing locks, and nothing is written back.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

#: name -> (config dir, keyring "application" attribute, executable for --version)
BROWSERS = {
    "chrome": ("~/.config/google-chrome", "chrome", "google-chrome-stable"),
    "chromium": ("~/.config/chromium", "chromium", "chromium"),
}

#: the cookies that clear a bot check, as opposed to the site's own login
CLEARANCE = re.compile(r"^(techaro\.lol-anubis-|cf_clearance$)")

_EPOCH_DELTA = 11644473600      # seconds from 1601-01-01 (Chrome) to 1970-01-01


class BrowserCookieError(Exception):
    pass


def _cookie_file(browser: str, profile: str) -> str:
    base = os.path.expanduser(BROWSERS[browser][0])
    for rel in ("Network/Cookies", "Cookies"):
        path = os.path.join(base, profile, rel)
        if os.path.exists(path):
            return path
    raise BrowserCookieError(
        f"no cookie file for {browser} profile {profile!r} under {base}")


def _key(password: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1, 16)


def _keyring_password(browser: str) -> bytes:
    app = BROWSERS[browser][1]
    try:
        out = subprocess.run(["secret-tool", "lookup", "application", app],
                             capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrowserCookieError(f"could not ask the keyring for the key: {exc}")
    pw = out.stdout.strip()
    if not pw:
        raise BrowserCookieError(
            f"no 'Safe Storage' key for {browser} in the keyring "
            "(is the keyring unlocked?)")
    return pw


def _decrypt(blob: bytes, host: str, keys: dict[bytes, bytes]) -> str:
    tag, body = blob[:3], blob[3:]
    key = keys.get(tag)
    if key is None or len(body) % 16:
        raise BrowserCookieError(f"unsupported cookie encryption {tag!r}")
    dec = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    padded = dec.update(body) + dec.finalize()
    unpad = padding.PKCS7(128).unpadder()
    plain = unpad.update(padded) + unpad.finalize()
    # newer builds prepend sha256(host_key) to the value
    if plain[:32] == hashlib.sha256(host.encode()).digest():
        plain = plain[32:]
    return plain.decode("utf-8")


def read_cookies(browser: str, domain: str, *, profile: str = "Default",
                 only: re.Pattern | None = None) -> dict[str, str]:
    """{name: value} for unexpired cookies of `domain` (and its subdomains)."""
    if browser not in BROWSERS:
        raise BrowserCookieError(f"unknown browser {browser!r} "
                                 f"(one of: {', '.join(BROWSERS)})")
    src = _cookie_file(browser, profile)
    now = time.time() + _EPOCH_DELTA
    with tempfile.TemporaryDirectory() as tmp:
        dst = os.path.join(tmp, "Cookies")
        shutil.copy(src, dst)
        db = sqlite3.connect(dst)
        try:
            rows = db.execute(
                "SELECT host_key, name, value, encrypted_value, expires_utc"
                " FROM cookies").fetchall()
        finally:
            db.close()
    keys: dict[bytes, bytes] = {}
    out: dict[str, str] = {}
    for host, name, value, blob, expires in rows:
        h = host.lstrip(".")
        if h != domain and not h.endswith("." + domain):
            continue
        if only and not only.search(name):
            continue
        if expires and expires / 1e6 < now:     # 0 = session cookie
            continue
        if not value and blob:
            tag = blob[:3]
            if tag not in keys:
                keys[tag] = _key(b"peanuts" if tag == b"v10"
                                 else _keyring_password(browser))
            value = _decrypt(blob, host, keys)
        out[name] = value
    return out


def user_agent(browser: str) -> str | None:
    """The browser's User-Agent, rebuilt from `--version` (Chrome freezes
    everything but the major version in it). Clearance cookies are tied to it."""
    try:
        v = subprocess.run([BROWSERS[browser][2], "--version"],
                           capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+)\.\d+\.\d+\.\d+", v)
    if not m:
        return None
    return ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{m.group(1)}.0.0.0 Safari/537.36")
