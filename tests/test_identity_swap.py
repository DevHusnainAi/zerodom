"""_SwapIdentity: a registered identity that sends with the real browser
fingerprint by swapping its cookie jar into the live tab per request. The one
thing that must never break is the restore — a mid-fetch failure must not leave
the live tab wearing another identity's cookies."""

import asyncio

import pytest

import zerodom.mcp_server as m


class FakeContext:
    def __init__(self, initial):
        self.jar = list(initial)

    async def cookies(self):
        return list(self.jar)

    async def clear_cookies(self):
        self.jar = []

    async def add_cookies(self, jar):
        self.jar = list(jar)


class FakePage:
    def __init__(self, ctx):
        self.context = ctx


ORIG = [{"name": "sid", "value": "A", "domain": "x", "path": "/"}]
B = [{"name": "sid", "value": "B", "domain": "x", "path": "/"}]


def _wire(monkeypatch, live_cls):
    ctx = FakeContext(ORIG)
    page = FakePage(ctx)

    async def fake_page():
        return page

    monkeypatch.setattr(m, "_page", fake_page)
    monkeypatch.setattr(m, "_LiveIdentity", live_cls)
    return ctx


def test_restores_original_jar_even_when_fetch_raises(monkeypatch):
    class BoomLive:
        def __init__(self, page):
            self.ctx = page.context

        async def fetch(self, *a, **k):
            assert self.ctx.jar == B  # B's cookies are installed during the fetch
            raise RuntimeError("boom")

    ctx = _wire(monkeypatch, BoomLive)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(m._SwapIdentity(B).fetch("https://x/api"))
    assert ctx.jar == ORIG  # restored despite the raise


def test_swaps_in_cookies_merges_headers_and_restores(monkeypatch):
    seen = {}

    class OkLive:
        def __init__(self, page):
            self.ctx = page.context

        async def fetch(self, url, method="GET", headers=None, data=None):
            seen["jar"] = list(self.ctx.jar)
            seen["headers"] = headers
            return m._InPageResponse(200, "ok", url, {})

    ctx = _wire(monkeypatch, OkLive)
    resp = asyncio.run(
        m._SwapIdentity(B, {"X-Test": "1"}).fetch("https://x/api", headers={"X-Req": "2"})
    )
    assert resp.status == 200
    assert seen["jar"] == B  # sent with B's cookies
    assert seen["headers"] == {"X-Test": "1", "X-Req": "2"}  # identity + per-call merged
    assert ctx.jar == ORIG  # restored after success too
