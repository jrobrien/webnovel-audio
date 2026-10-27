"""Stored browser cookies for a provider with logged-in content.

Deliberately never *verified* up front: that needs an account page whose
markup drifts, and a stale cookie shows up honestly at the point of use (a
locked chapter fails to fetch). Nothing here reads account state.
"""
from __future__ import annotations

import json
import os

CONFIG_DIR = os.path.expanduser("~/.config/webnovel-audio")


class CookieSession:
    def __init__(self, filename: str, domain: str):
        self.filename = filename
        self.domain = domain

    @property
    def path(self) -> str:
        return os.path.join(CONFIG_DIR, self.filename)

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh).get("cookies", {})
        except (OSError, json.JSONDecodeError):
            return {}

    def user_agent(self) -> str | None:
        """The User-Agent the cookies were earned under, if one was stored.
        Bot-check clearance cookies are only honoured from the same one."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh).get("user_agent") or None
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, cookies: dict, user_agent: str | None = None) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        doc = {"cookies": cookies}
        if user_agent:
            doc["user_agent"] = user_agent
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.chmod(self.path, 0o600)

    def merge(self, cookies: dict, user_agent: str | None = None) -> None:
        """Add to what is stored (a login cookie survives a clearance refresh)."""
        self.save({**self.load(), **cookies}, user_agent or self.user_agent())

    def clear(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass

    def parse_cookies_txt(self, path: str) -> dict:
        """Netscape cookies.txt -> {name: value} for this session's domain."""
        out: dict[str, str] = {}
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                cols = line.split("\t")
                if len(cols) >= 7 and self.domain in cols[0]:
                    out[cols[5]] = cols[6]
        return out


def parse_cookie_header(s: str) -> dict:
    out: dict[str, str] = {}
    for part in s.strip().strip(";").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out
