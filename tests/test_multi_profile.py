"""Multi-profile live identities: a second real Chrome profile registered as an
identity over its own relay endpoint, driven via its own isolated browser
connection. The things that must hold: the connection is isolated from the
active session's pw (so active-tab teardown can't kill it), requests hit the
second profile's page, and compare fires independent identities concurrently
but falls back to sequential when a cookie-swap identity (shared single jar) is
in the set."""

import asyncio

import pytest

import zerodom.mcp_server as m


# ─── fakes ───────────────────────────────────────────────────────────────────

class FakePage:
    def __init__(self, tag):
        self.tag = tag
        self.seen = []

    async def evaluate(self, js, arg=None):
        self.seen.append(arg)
        return {"ok": True, "status": 200, "url": arg["url"], "headers": {},
                "body": f"body-from-{self.tag}"}


class FakeContext:
    def __init__(self, page):
        self.pages = [page]

    async def new_page(self):  # not expected here (one tab already open)
        raise AssertionError("should reuse the already-open tab")


class FakeBrowser:
    def __init__(self, page):
        self.contexts = [FakeContext(page)]


class FakePw:
    def __init__(self):
        self.stopped = False
        self._page = FakePage("profileB")

        class _Chromium:
            connected_to = []

            async def connect_over_cdp(_self, endpoint, timeout=None):
                _self.connected_to.append(endpoint)
                return FakeBrowser(self._page)

        self.chromium = _Chromium()

    async def stop(self):
        self.stopped = True


def _patch_pw(monkeypatch, fake_pw):
    class Ctx:
        async def start(self):
            return fake_pw
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: Ctx())


@pytest.fixture(autouse=True)
def clean_identities():
    reset = dict(pw=None, browser=None, active=None, pages={}, url=None,
                 identities={}, identity_conns={}, scope=None,
                 profiles={"default": {"endpoint": None, "state": None}},
                 current_profile="default")
    m._session.update(reset)
    yield
    m._session.update(pw=None, browser=None, active=None, pages={}, url=None,
                      identities={}, identity_conns={}, scope=None,
                      profiles={"default": {"endpoint": None, "state": None}},
                      current_profile="default")


# ─── endpoint registration ─────────────────────────────────────────────────────

def test_endpoint_registers_isolated_profile_identity(monkeypatch):
    fake_pw = FakePw()
    _patch_pw(monkeypatch, fake_pw)

    out = asyncio.run(m.zerodom_add_identity("B", endpoint="ws://127.0.0.1:8766/cdp/local"))

    assert "live second profile" in out
    assert isinstance(m._session["identities"]["B"], m._ProfileIdentity)
    assert fake_pw.chromium.connected_to == ["ws://127.0.0.1:8766/cdp/local"]
    # isolated: the profile's pw is its own, never the active session's.
    assert m._session["pw"] is None
    assert m._session["identity_conns"]["B"]["pw"] is fake_pw


def test_endpoint_identity_fetch_hits_the_second_profile(monkeypatch):
    fake_pw = FakePw()
    _patch_pw(monkeypatch, fake_pw)
    asyncio.run(m.zerodom_add_identity("B", endpoint="ws://127.0.0.1:8766/cdp/local"))

    req = asyncio.run(m._identity_request("B"))
    resp = asyncio.run(req.fetch("https://example.com/api"))
    assert asyncio.run(resp.text()) == "body-from-profileB"  # the 2nd browser's page, not live


def test_endpoint_is_mutually_exclusive(monkeypatch):
    _patch_pw(monkeypatch, FakePw())
    out = asyncio.run(m.zerodom_add_identity("B", endpoint="ws://x/cdp/local", from_live=True))
    assert "don't combine it with" in out
    assert "B" not in m._session["identities"]  # rejected before any connect


def test_teardown_leaves_profile_conns_alive(monkeypatch):
    fake_pw = FakePw()
    _patch_pw(monkeypatch, fake_pw)
    asyncio.run(m.zerodom_add_identity("B", endpoint="ws://127.0.0.1:8766/cdp/local"))

    asyncio.run(m._teardown_active_session())

    assert "B" in m._session["identity_conns"]  # active-tab teardown must not touch it
    assert fake_pw.stopped is False


# ─── compare concurrency ────────────────────────────────────────────────────────

def _recorder(tag, order):
    async def fetch(url, method="GET", data=None):
        order.append(f"start-{tag}")
        await asyncio.sleep(0)  # yield: lets a concurrent sibling start before we finish
        order.append(f"end-{tag}")
        return m._InPageResponse(200, f"x-{tag}", url, {})
    return fetch


class FakeId:
    def __init__(self, tag, order):
        self.fetch = _recorder(tag, order)


def test_compare_fires_independent_identities_concurrently():
    order = []
    m._session["identities"] = {"A": FakeId("A", order), "B": FakeId("B", order)}
    asyncio.run(m.zerodom_compare_identities("https://example.com/x", identities=["A", "B"]))
    # both start before either ends → gather, not a sequential loop
    assert order == ["start-A", "start-B", "end-A", "end-B"]


def test_switch_profile_registers_and_routes_the_endpoint(monkeypatch):
    monkeypatch.delenv("ZERODOM_CDP_ENDPOINT", raising=False)
    out = asyncio.run(m.zerodom_switch_profile("B", endpoint="ws://127.0.0.1:8766/cdp/local"))
    assert "switched to profile 'B'" in out
    assert m._session["current_profile"] == "B"
    # _page() connects THIS profile's endpoint while it's active
    assert m._cdp_endpoint() == "ws://127.0.0.1:8766/cdp/local"
    # back to default → env endpoint (None here)
    asyncio.run(m.zerodom_switch_profile("default"))
    assert m._session["current_profile"] == "default"
    assert m._cdp_endpoint() is None


def test_switch_profile_preserves_each_profiles_page_state(monkeypatch):
    m._session["url"] = "https://a.example/page"
    m._session["active"] = "0"
    asyncio.run(m.zerodom_switch_profile("B", endpoint="ws://127.0.0.1:8766/cdp/local"))
    # fresh profile starts blank so _page() bootstraps its own connection
    assert m._session["url"] is None and m._session["active"] is None
    m._session["url"] = "https://b.example/page"  # simulate driving B
    asyncio.run(m.zerodom_switch_profile("default"))
    assert m._session["url"] == "https://a.example/page"  # A restored exactly
    asyncio.run(m.zerodom_switch_profile("B"))
    assert m._session["url"] == "https://b.example/page"  # B restored exactly


def test_switch_profile_rejects_unknown_and_noops_same():
    assert "unknown profile 'ghost'" in asyncio.run(m.zerodom_switch_profile("ghost"))
    assert "already on profile 'default'" in asyncio.run(m.zerodom_switch_profile("default"))


def test_compare_stays_sequential_when_a_swap_identity_is_present():
    order = []
    swap = m._SwapIdentity([])          # real type → triggers the guard
    swap.fetch = _recorder("B", order)  # instance attr shadows the real method
    m._session["identities"] = {"A": FakeId("A", order), "B": swap}
    asyncio.run(m.zerodom_compare_identities("https://example.com/x", identities=["A", "B"]))
    # each runs to completion before the next starts — no interleave
    assert order == ["start-A", "end-A", "start-B", "end-B"]
