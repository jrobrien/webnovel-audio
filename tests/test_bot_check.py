import re

import httpx
import pytest

from webnovel_audio.providers.http import PoliteClient


def test_anubis_page_is_reported_not_parsed():
    page = '<title>Making sure</title><script id="anubis_challenge" type="application/json">{}</script>'
    c = PoliteClient(delay=0, label="royalroad")
    c._c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page)))
    with pytest.raises(SystemExit, match=re.compile("Anubis.*ingest", re.S)):
        c.get("https://www.royalroad.com/fiction/1")


def test_cloudflare_challenge_is_reported_with_the_way_out():
    page = "<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"
    c = PoliteClient(delay=0, label="royalroad")
    c._c = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(403, text=page, headers={"cf-mitigated": "challenge"})))
    with pytest.raises(SystemExit, match=re.compile("Cloudflare.*ingest", re.S)):
        c.get("https://www.royalroad.com/fiction/1")
