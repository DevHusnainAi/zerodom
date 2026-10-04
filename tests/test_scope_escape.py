"""Scope-escape suite — HYPOTHESIS-ACK-PLAN §3.3 (T4.3).

Table-driven: each row is a way a locked scope could be walked out of, and the
assertion is always the same — **refused**. Phase 4 is not done until every row
here passes, plus the negative controls proving the guard refuses *only* the
escape (in-scope actions still run).
"""

from __future__ import annotations

import asyncio

import pytest

from zerodom import mcp_server

LOCKED_SCOPE = {
    "allow": ["app.test"],
    "deny": mcp_server._SCOPE_DEFAULT_DENY,
    "max_rps": None,
    "locked": True,
    "_last": 0.0,
}


class FakeLocator:
    """What FakePage.locator() returns — enough for _act to dispatch a click."""

    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    async def count(self):
        return 1

    async def evaluate(self, js, arg=None):
        return None  # no identity surface → _identity_stale treats as agreeable

    async def click(self, *a, **kw):
        self.page.clicked.append(self.selector)


class FakePage:
    """Navigation that can be made to "redirect", plus evaluate() for eval_js.

    Deliberately has NO .content(): attempt_clearance's fake-page bail-out then
    skips the clearance ladder, so these tests exercise scope alone."""

    def __init__(self, land_on: str | None = None):
        self.url = "about:blank"
        self.land_on = land_on  # server-side redirect target, if any
        self.clicked: list[str] = []

    def is_closed(self):
        return False

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def goto(self, url, **kw):
        self.url = self.land_on or url

    async def wait_for_load_state(self, state=None, timeout=None):
        pass

    async def evaluate(self, js, arg=None):
        return 2  # canned — the fake has no JS engine; call shapes are what matter

    def on(self, event, handler):
        pass


@pytest.fixture
def session():
    """A locked scope (app.test only, destructive denied) and a clean session."""
    keys = ("scope", "nodes", "pages", "active", "attached", "identities",
            "selectors", "url")
    saved = {k: mcp_server._session.get(k) for k in keys}
    mcp_server._session.update(
        scope=dict(LOCKED_SCOPE), nodes=[], pages={}, active=None,
        attached=False, identities={}, selectors={}, url=None,
    )
    yield
    mcp_server._session.update(saved)


def _install(page) -> None:
    mcp_server._session.update(pages={"0": page}, active="0")


# ─── The escape vectors: every one must be refused ──────────────────────────

def _out_of_scope_parse_url():
    _install(FakePage())
    return mcp_server.zerodom_parse_url("https://evil.test/x")


def _redirect_out_of_scope():
    # Starts in scope; the "server" 302s to an out-of-scope host (the
    # chrome-devtools-mcp redirect-bypass bug shape, PROFILE-A-PLAN §F2).
    _install(FakePage(land_on="https://evil.test/landing"))
    return mcp_server.zerodom_parse_url("https://app.test/start")


def _destructive_deny_click():
    _install(FakePage())
    mcp_server._session["nodes"] = [
        {"id": "node_01", "type": "button", "label": "Delete account",
         "selector": "#del", "href": ""},
    ]
    return mcp_server._act("01", "click")


def _eval_js_navigation():
    _install(FakePage())
    return mcp_server.zerodom_eval_js('location.href = "https://evil.test/x"')


ESCAPE_VECTORS = [
    ("out-of-scope parse_url", _out_of_scope_parse_url, "out of scope"),
    ("redirect to out-of-scope host", _redirect_out_of_scope, "out of scope"),
    ("destructive deny-rule click", _destructive_deny_click, "destructive"),
    ("eval_js navigation", _eval_js_navigation, "out of scope"),
]


@pytest.mark.parametrize(
    ("name", "attempt", "match"), ESCAPE_VECTORS,
    ids=[v[0] for v in ESCAPE_VECTORS],
)
def test_escape_vector_is_refused(session, name, attempt, match):
    with pytest.raises(PermissionError, match=match):
        asyncio.run(attempt())


@pytest.mark.parametrize(
    "code",
    [
        'location.assign("https://evil.test/a")',
        'location.replace("https://evil.test/b")',
        'window.open("https://evil.test/c")',
        "location = 'https://evil.test/d'",
        'location.href = "//evil.test/e"',  # protocol-relative still leaves
    ],
    ids=["location.assign", "location.replace", "window.open", "location", "protocol-relative"],
)
def test_every_eval_js_navigation_primitive_is_refused(session, code):
    _install(FakePage())
    with pytest.raises(PermissionError, match="out of scope"):
        asyncio.run(mcp_server.zerodom_eval_js(code))


# ─── Negative controls: the guard refuses only the escape ───────────────────

def test_in_scope_eval_js_still_runs(session):
    _install(FakePage())
    assert asyncio.run(mcp_server.zerodom_eval_js("1 + 1")) == "2"
    # Navigating *within* scope is not an escape — allowed, not blanket-blocked.
    assert asyncio.run(
        mcp_server.zerodom_eval_js('location.href = "https://app.test/next"')) == "2"


def test_in_scope_click_still_runs(session):
    _install(FakePage())
    mcp_server._session["nodes"] = [
        {"id": "node_01", "type": "a", "label": "View", "selector": "#v", "href": "/view"},
    ]
    asyncio.run(mcp_server._act("01", "click"))
    assert mcp_server._session["pages"]["0"].clicked == ["#v"]


def test_in_scope_parse_url_still_navigates(session):
    page = FakePage()
    _install(page)
    # The redirect target is in scope → navigation happens, no refusal.
    # (A full _read() needs a real serializer; the navigation itself is what
    # this row asserts — _goto completed and the URL is where we asked.)
    asyncio.run(mcp_server._goto(page, "https://app.test/dashboard"))
    assert page.url == "https://app.test/dashboard"
