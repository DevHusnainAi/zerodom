"""Phase-0 clearance ladder tests (HYPOTHESIS-ACK-PLAN T0.5).

Rung ordering, kill switches, scope refusal, and the solver flow run against a
fake page + a local fake provider — no browser, no external network (CI).
The two browser-backed tests (real Chromium fixture wall, cdp_pipe no-Runtime
regression) are gated the same way test_playwright.py gates.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from zerodom import clearance, mcp_server, solvers
from zerodom.scope import check_scope

WALL = """
<html><head><script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></head>
<body><form><div class="cf-turnstile" data-sitekey="0xAAAAAAAA"></div>
<input name="q"><button>Go</button></form></body></html>
"""

CLEAN = """
<html><title>App</title><body><form><input name="q"><button>Go</button></form></body></html>
"""


class FakeContext:
    def __init__(self, page, cookies=None):
        self._page = page
        self._cookies = list(cookies or [])

    def cookies(self):
        return list(self._cookies)

    def add_cookies(self, jar):
        self._cookies.extend(jar)
        self._page.solved = True  # a landed clearance cookie is what unblocks


class FakeSyncPage:
    """Wall page whose server honors a carried cf_clearance cookie on re-nav.

    First read always serves `html` (the wall the caller just detected); a
    later read serves CLEAN only once the context carries a clearance cookie —
    exactly the local fixture-wall server's contract below.
    """

    def __init__(self, cookies=None, html=WALL, url="https://target.example/app"):
        self.html = html
        self.url = url
        self.solved = False
        self.gotos = []
        self.context = FakeContext(self, cookies)

    def _carries(self):
        return any(c.get("name") == "cf_clearance" for c in self.context.cookies())

    def content(self):
        if self.solved or (self.gotos and self._carries()):
            return CLEAN
        return self.html

    def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = url

    def evaluate(self, script, arg=None):
        return None


class SolverServer:
    """Local fake provider: POST → {"cookies":[...]} or {"token": ...}, counts hits."""

    def __init__(self, reply):
        self.reply = reply
        self.hits = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.hits += 1
                body = json.dumps(outer.reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # keep test output clean
                pass

        self._srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self._srv.server_port}/solve"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture(autouse=True)
def clean_policy(monkeypatch):
    """Isolate the ladder from host env/defaults left by other tests or the dev box."""
    for var in ("ZERODOM_SOLVER_PROVIDER", "ZERODOM_SOLVER_KEY", "ZERODOM_SOLVER_URL",
                "ZERODOM_NO_SOLVER", "ZERODOM_SCOPE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(clearance._DEFAULTS, "no_solver", False)
    clearance.take_cleared()
    clearance.take_notes()


# -- pure ladder behavior ---------------------------------------------------

def test_no_wall_is_a_noop():
    page = FakeSyncPage(html=CLEAN)
    assert clearance.attempt_clearance(page, None, clearance.make_policy()) is None
    assert clearance.take_cleared() is None


def test_rung_one_reused_clearance_wins_with_a_carried_cookie():
    page = FakeSyncPage(cookies=[{"name": "cf_clearance", "value": "abc"}])
    result = clearance.attempt_clearance(page, None, clearance.make_policy(url=page.url))
    assert result["rung"] == "reused"
    assert result["wall_kind"] == "turnstile"
    assert result["attempts"] == 1
    assert set(result) >= {"rung", "wall_kind", "attempts", "seconds"}
    assert page.gotos, "rung 1 must re-navigate so the carried cookie is sent"
    assert clearance.take_cleared() == result


def test_ladder_counts_attempts_across_failed_rungs(monkeypatch):
    server = SolverServer({"cookies": [{"name": "cf_clearance", "value": "x"}]})
    monkeypatch.setenv("ZERODOM_SOLVER_URL", server.url)
    try:
        page = FakeSyncPage()  # no cookie → rung 1 fails → rung 3 clears
        policy = clearance.make_policy(url=page.url, rungs=("reused", "solver"))
        result = clearance.attempt_clearance(page, None, policy)
    finally:
        server.close()
    assert result["rung"] == "solver"
    assert result["attempts"] == 2  # reused failed, solver tried
    assert result["solver"]["provider"] == "custom"


def test_solver_flow_injects_cookies_and_cleans_the_page(monkeypatch):
    server = SolverServer({"cookies": [{"name": "cf_clearance", "value": "tok"}],
                           "cost": 0.001})
    monkeypatch.setenv("ZERODOM_SOLVER_URL", server.url)
    try:
        page = FakeSyncPage()
        result = clearance.attempt_clearance(page, None, clearance.make_policy(url=page.url))
    finally:
        server.close()
    assert result is not None and result["rung"] == "solver"
    assert result["solver"]["cost"] == 0.001
    assert page.solved and page.gotos, "rung 3 must re-nav after injecting cookies"


def test_solver_never_fires_out_of_scope(monkeypatch):
    server = SolverServer({"token": "nope"})
    monkeypatch.setenv("ZERODOM_SOLVER_URL", server.url)
    try:
        page = FakeSyncPage(url="https://other.example/app")
        policy = clearance.make_policy(
            url=page.url, scope={"allow": ["allowed.example"], "deny": None})
        result = clearance.attempt_clearance(page, None, policy)
    finally:
        server.close()
    assert result is None
    assert server.hits == 0, "an out-of-scope wall must not reach the provider"
    assert any("out of scope" in n for n in clearance.take_notes())


def test_no_solver_policy_disables_rung_three(monkeypatch):
    server = SolverServer({"token": "nope"})
    monkeypatch.setenv("ZERODOM_SOLVER_URL", server.url)
    try:
        page = FakeSyncPage()
        result = clearance.attempt_clearance(
            page, None, clearance.make_policy(url=page.url, no_solver=True))
    finally:
        server.close()
    assert result is None
    assert server.hits == 0


def test_env_kill_switch_beats_everything(monkeypatch):
    monkeypatch.setenv("ZERODOM_NO_SOLVER", "1")
    assert solvers.solver_disabled() is True
    assert solvers.get_solver() is None
    monkeypatch.setenv("ZERODOM_SOLVER_URL", "http://127.0.0.1:1/solve")
    assert solvers.get_solver() is None  # the switch, not the config, decides


def test_human_rung_waits_for_the_operator_on_relay_only():
    # Playwright transport: the human rung must not even be considered.
    page = FakeSyncPage()
    policy = clearance.make_policy(url=page.url, no_solver=True)
    assert clearance.attempt_clearance(page, None, policy) is None

    # Relay transport: the page clears during the bounded human window.
    page = FakeSyncPage()
    flips = {"n": 0}
    real_content = page.content

    def content():
        flips["n"] += 1
        return CLEAN if flips["n"] > 1 else real_content()

    page.content = content
    policy = clearance.make_policy(url=page.url, transport="relay", no_solver=True)
    result = clearance.attempt_clearance(page, None, policy)
    assert result is not None and result["rung"] == "human"
    assert any("clear it in your browser" in n for n in clearance.take_notes())


def test_scope_check_shapes():
    scope = {"allow": ["app.example"], "deny": r"logout|/delete"}
    check_scope("https://app.example/x", scope)  # in scope: no raise
    with pytest.raises(PermissionError, match="deny rule"):
        check_scope("https://app.example/logout", scope)
    with pytest.raises(PermissionError, match="out of scope"):
        check_scope("https://evil.example/x", scope)
    check_scope("https://anything.example", None)  # no scope = unrestricted


def test_sitekey_extracted_from_widget_markup():
    assert solvers.sitekey(WALL) == "0xAAAAAAAA"


def test_config_set_get_masks_the_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(solvers, "CONFIG_PATH", tmp_path / "config.json")
    from zerodom.cli import config_cmd

    class A:
        config_action = "set"
        key = "solver.key"
        value = "SECRETKEY123456789"

    assert config_cmd(A) == 0
    out = capsys.readouterr().out
    assert "SECRETKEY123456789" not in out and "masked" in out
    A.config_action = "get"
    assert config_cmd(A) == 0
    out = capsys.readouterr().out
    assert "SECRETKEY123456789" not in out and "masked" in out


# -- MCP surfacing ----------------------------------------------------------

WALL_PAGE = WALL  # readable alias


class _FakeMCPPage:
    def __init__(self, html):
        self.html = html
        self.url = "https://example.com/"

    def is_closed(self):
        return False

    async def content(self):
        return self.html

    async def goto(self, url, **kw):
        self.url = url

    async def evaluate(self, script, arg=None):
        return None

    async def wait_for_load_state(self, state=None):
        return None

    def locator(self, selector):  # not exercised by these tests
        raise AssertionError("no actions in clearance tests")


@pytest.fixture
def mcp_page():
    fake = _FakeMCPPage(WALL_PAGE)
    saved = {k: mcp_server._session.get(k) for k in
             ("pages", "active", "selectors", "nodes", "url", "blocked", "cleared",
              "clearance", "clearance_note", "attached", "frames", "viewport_only",
              "check_occlusion")}
    mcp_server._session.update(pages={"0": fake}, active="0", selectors={},
                               nodes=[], url=None, clearance=None, clearance_note=None)
    yield fake
    for k, v in saved.items():
        mcp_server._session[k] = v


def test_mcp_surfaces_blocked_and_cleared(mcp_page):
    out = asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    assert mcp_server._session.get("blocked", {}).get("kind") == "turnstile"
    assert "[blocked turnstile" in out  # the tail line the agent reads

    # Cleared rides the same line: inject the ladder's result as _goto would.
    mcp_server._session["clearance"] = {"rung": "reused", "wall_kind": "turnstile",
                                        "attempts": 1, "seconds": 0.1}
    mcp_page.html = CLEAN
    out = asyncio.run(mcp_server._read())
    assert "[cleared turnstile — rung reused" in out
    assert mcp_server._session["cleared"]["rung"] == "reused"


# -- browser-backed (gated the way test_playwright.py gates) ----------------

_NEEDS_CHROMIUM = pytest.mark.skipif(
    not list(Path.home().glob(".cache/ms-playwright/chromium*")),
    reason="chromium not installed (run: uv run playwright install chromium)")


@_NEEDS_CHROMIUM
def test_real_browser_reused_cookie_clears_a_local_wall():
    playwright = pytest.importorskip("playwright.sync_api")

    hits = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits["n"] += 1
            cookie = self.headers.get("Cookie", "")
            # First read is the wall the ladder was invoked for; a re-nav
            # carries cf_clearance and gets the real page — same contract as
            # FakeSyncPage above.
            if hits["n"] > 1 and "cf_clearance=good" in cookie:
                body = CLEAN.encode()
            else:
                body = WALL.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/"
    try:
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context()
            ctx.add_cookies([{"name": "cf_clearance", "value": "good",
                              "url": url}])
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded")
            result = clearance.attempt_clearance(page, None, clearance.make_policy(url=url))
            browser.close()
    finally:
        srv.shutdown()
        srv.server_close()
    assert result is not None and result["rung"] == "reused", result


@_NEEDS_CHROMIUM
def test_cdppipe_reads_html_without_the_runtime_domain():
    """Rung-2 hygiene regression: the stealth transport must never send Runtime.*
    (the mid-load CDP signal bot vendors fingerprint — rebrowser-patches)."""
    from zerodom import cdp_pipe

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<html><title>Pipe</title><body><a href='/x'>link</a></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/"
    sent = []
    original = cdp_pipe.CDPPipe.send

    def spy(self, method, params=None, **kw):
        sent.append(method)
        return original(self, method, params, **kw)

    cdp_pipe.CDPPipe.send = spy
    try:
        html, final = cdp_pipe.fetch(url)
    finally:
        cdp_pipe.CDPPipe.send = original
        srv.shutdown()
        srv.server_close()
    assert "Pipe" in html and final.rstrip("/").endswith(str(srv.server_port))
    leaked = [m for m in sent if m.startswith("Runtime.")]
    assert not leaked, f"stealth transport sent {leaked}"
