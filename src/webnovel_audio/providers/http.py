"""A polite HTTP client shared by the web providers.

Politeness is a property of us-vs-a-server, not of a client object: a provider
may build a fresh client per chapter, and a per-instance timestamp would let
`fetch <slug> 1-50` fire 50 requests with no gap. So the last-request time is
kept per host at class level.
"""
from __future__ import annotations

import threading
import time

import httpx

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)


class PoliteClient:
    _last_request: dict[str, float] = {}
    _rate_lock = threading.Lock()

    def __init__(self, *, delay: float = 2.5, cookies: dict | None = None,
                 headers: dict | None = None, label: str = "http"):
        self.delay = delay
        self.label = label
        self._c = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9",
                     **(headers or {})},
            cookies=cookies or {},
            follow_redirects=True,
            timeout=30.0,
        )

    def _wait_turn(self, host: str) -> None:
        with PoliteClient._rate_lock:
            gap = time.monotonic() - PoliteClient._last_request.get(host, 0.0)
            if gap < self.delay:
                time.sleep(self.delay - gap)
            PoliteClient._last_request[host] = time.monotonic()

    def get(self, url: str, *, headers: dict | None = None, tries: int = 4) -> httpx.Response:
        host = (httpx.URL(url).host or "").lower()
        self._wait_turn(host)
        last_exc: Exception | None = None
        for attempt in range(tries):
            try:
                resp = self._c.get(url, headers=headers)
                PoliteClient._last_request[host] = time.monotonic()
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(self.delay * (attempt + 2))
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:  # pragma: no cover - network
                last_exc = exc
                time.sleep(self.delay * (attempt + 2))
        raise SystemExit(f"{self.label}: giving up on {url} ({last_exc})")

    def text(self, url: str, *, headers: dict | None = None) -> str:
        return self.get(url, headers=headers).text

    def close(self) -> None:
        self._c.close()


def fetch_asset(url: str, hosts: tuple[str, ...], *, timeout: float = 15.0) -> bytes | None:
    """GET an image **without** any session cookie, and only from `hosts` (or
    their subdomains). Cover URLs come from page metadata; using a logged-in
    client would leak the cookie to an arbitrary server."""
    try:
        host = (httpx.URL(url).host or "").lower()
    except Exception:  # noqa: BLE001
        return None
    if not any(host == h or host.endswith("." + h) for h in hosts):
        return None
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT},
                      follow_redirects=True, timeout=timeout)
        r.raise_for_status()
        return r.content
    except httpx.HTTPError:
        return None
