import asyncio

import pytest

from zerodom import mcp_server

PAGE = """
<html><title>T</title><body>
  <input id="q" aria-label="Search">
  <button id="go">Go</button>
  <button id="menu">Menu</button>
</body></html>
"""

# What the page becomes after the click navigates.
NEXT_PAGE = """
<html><title>Results</title><body><a href="/back" id="back">Back</a></body></html>
"""


class FakeLocator:
    """What `frames.locate()` returns for a FakePage: actions route straight back
    to the page, so existing call assertions keep working unchanged."""

    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    async def click(self, *a, **kw):
        await self.page.click(self.selector)

    async def fill(self, text, *a, **kw):
        await self.page.fill(self.selector, text)


class FakeMouse:
    def __init__(self, page):
        self.page = page

    async def wheel(self, dx, dy):
        self.page.calls.append(("wheel", dx, dy))


class FakePage:
    """Stands in for a Playwright page so MCP tools are testable without a browser."""

    def __init__(self):
        self.calls = []
        self.url = "https://example.com/"
        self.html = PAGE
        self.typed: dict[str, str] = {}
        self.mouse = FakeMouse(self)
        self.sidebar_eval = None

    def is_closed(self):
        return False

    async def goto(self, url, **kw):
        self.calls.append(("goto", url))
        self.url, self.html, self.typed = url, PAGE, {}

    async def content(self):
        return self.html

    async def evaluate(self, script, arg=None):
        # The SERIALIZE shadow-root probe should stay a wash (fall back to
        # content()); the framework-root hydration probe answers yes so the
        # post-navigation "handler may not have finished attaching" hint in
        # zerodom_click_node is genuinely exercised rather than silently dead.
        if "hydrationPending" in script:
            return {"hydrationPending": True}
        if "__zerodomNodes" in script:
            self.sidebar_eval = arg
            return None
        return None

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def wait_for_load_state(self, state=None):
        self.calls.append(("wait_for_load_state", state))

    async def click(self, selector):
        self.calls.append(("click", selector))
        if selector == "#menu":  # opens something in place, like a real menu
            self.html = PAGE.replace("</body>", '<a href="/x" id="opened">Opened</a></body>')
        else:
            self.url, self.html = "https://example.com/results", NEXT_PAGE

    async def fill(self, selector, text):
        self.calls.append(("fill", selector, text))
        self.typed[selector] = text
        # A real fill lands in the DOM, so the next parse sees the new value.
        self.html = PAGE.replace('<input id="q"', f'<input id="q" value="{text}"')


@pytest.fixture
def page(monkeypatch):
    fake = FakePage()
    mcp_server._session.update(pages={"0": fake}, active="0", selectors={}, nodes=[], url=None)
    yield fake
    mcp_server._session.update(pages={}, active=None, selectors={}, nodes=[], url=None)


def test_tools_are_registered():
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {
        "zerodom_parse_url",
        "zerodom_read_page",
        "zerodom_find",
        "zerodom_click_node",
        "zerodom_fill_node",
    } <= names


def test_parse_url_returns_compact_graph_and_hides_selectors(page):
    out = asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    assert out.splitlines() == [
        "PAGE: T | https://example.com/",
        "[01] input 'Search'",
        "[02] button 'Go'",
        "[03] button 'Menu'",
    ]
    # Selectors are held server-side, not spent on model context.
    assert "#q" not in out
    assert mcp_server._session["selectors"] == {
        "node_01": "#q", "node_02": "#go", "node_03": "#menu",
    }
    assert ("goto", "https://example.com/") in page.calls


def test_parse_url_verbose_returns_full_json(page):
    out = asyncio.run(mcp_server.zerodom_parse_url("https://example.com/", verbose=True))
    assert '"selector": "#q"' in out


def test_click_and_fill_resolve_node_ids(page):
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    asyncio.run(mcp_server.zerodom_fill_node("node_01", "zerodom"))
    asyncio.run(mcp_server.zerodom_click_node("node_02"))
    assert ("fill", "#q", "zerodom") in page.calls
    assert ("click", "#go") in page.calls


def test_scroll_dispatches_a_wheel_event_and_returns_a_diff(page):
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    out = asyncio.run(mcp_server.zerodom_scroll())
    assert ("wheel", 0, 800) in page.calls
    assert out == "no structural change"


def test_scroll_up_uses_a_negative_delta(page):
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    asyncio.run(mcp_server.zerodom_scroll(direction="up", amount=400))
    assert ("wheel", 0, -400) in page.calls


def test_read_page_does_not_navigate(page):
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    asyncio.run(mcp_server.zerodom_fill_node("node_01", "typed text"))
    page.calls.clear()

    out = asyncio.run(mcp_server.zerodom_read_page())

    assert not [c for c in page.calls if c[0] == "goto"], "re-read must not reload"
    assert page.typed == {"#q": "typed text"}, "typed state survived the re-read"
    assert out.startswith("PAGE: T | https://example.com/")


def test_read_page_refreshes_stale_node_ids(page):
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    page.url, page.html = "https://example.com/results", NEXT_PAGE

    out = asyncio.run(mcp_server.zerodom_read_page())

    assert out.splitlines() == ["PAGE: Results | https://example.com/results", "[01] a 'Back'"]
    assert mcp_server._session["selectors"] == {"node_01": "#back"}


def test_click_returns_the_graph_of_the_page_it_landed_on(page):
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    out = asyncio.run(mcp_server.zerodom_click_node("02"))

    # One turn: the model sees what its click produced without a second call.
    assert out.splitlines()[0] == "clicked [02] -> https://example.com/results"
    assert "PAGE: Results | https://example.com/results" in out
    assert "[01] a 'Back'" in out
    # And the ids it just read are the ones the server will resolve next.
    assert mcp_server._session["selectors"] == {"node_01": "#back"}


def test_fill_returns_the_refreshed_graph_without_navigating(page):
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    out = asyncio.run(mcp_server.zerodom_fill_node("01", "zerodom"))

    assert out.splitlines()[0] == "filled [01] with 'zerodom'"
    assert "~[01] input 'Search'  '' -> 'zerodom'" in out
    assert not [c for c in page.calls if c[0] == "goto"][1:], "fill must not reload"


@pytest.mark.parametrize("node_id", ["1", "01", "[01]", "node_01"])
def test_bare_indexes_from_the_compact_graph_resolve(page, node_id):
    """The compact graph prints `[01]`, so the model will send back `01` or `1`."""
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    asyncio.run(mcp_server.zerodom_click_node(node_id))
    assert ("click", "#q") in page.calls


def test_unknown_node_id_is_rejected(page):
    with pytest.raises(ValueError, match="Unknown node"):
        asyncio.run(mcp_server.zerodom_click_node("node_99"))


def test_find_returns_only_matching_nodes(page):
    """The point of find: three lines instead of every node on the page."""
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    out = asyncio.run(mcp_server.zerodom_find("menu"))
    assert out == "[03] button 'Menu'"


def test_find_matches_all_terms_case_insensitively(page):
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    assert asyncio.run(mcp_server.zerodom_find("BUTTON go")) == "[02] button 'Go'"


def test_find_reports_a_miss_without_dumping_the_page(page):
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    out = asyncio.run(mcp_server.zerodom_find("checkout"))
    assert "No node matches" in out and "Go" not in out


def test_sensitive_field_regex_matches_the_ui_prefix():
    """The server fill-refusal must flag exactly the fields the UI marks
    (graph_ui.js SENSITIVE_RE uses the `passw` prefix, not the full word
    `password`). The drift this closes: a label like the German `Passwort`
    contains `passw` — the UI refused it, the server used to fill it.
    Under-refusal is the security-relevant direction (CLAUDE.md contract)."""
    for label in ("Password", "Passwort", "passwd", "CVV", "IBAN"):
        assert mcp_server._is_sensitive_field({"label": label}), label
    # a plain field is still fillable
    assert not mcp_server._is_sensitive_field({"label": "Search"})


def test_find_before_any_page_is_loaded(page):
    assert "No page loaded" in asyncio.run(mcp_server.zerodom_find("x"))


HIDDEN_HTML = """
<html><body>
  <form>
    <input type="hidden" name="csrf_token" value="abc123">
    <input type="hidden" id="draft_id" value="42">
    <input id="q" aria-label="Search">
    <button id="go">Go</button>
  </form>
</body></html>
"""


def test_hidden_fields_do_not_become_graph_nodes(page):
    """A hidden input isn't clickable or fillable, so it must not be a node."""
    page.html = HIDDEN_HTML
    asyncio.run(mcp_server._read())
    labels = [n["label"] for n in mcp_server._session["nodes"]]
    assert "csrf_token" not in labels and "draft_id" not in labels
    assert mcp_server._session["hidden_fields"] == [
        {"name": "csrf_token", "value": "abc123", "selector": "input[name='csrf_token']"},
        {"name": "", "value": "42", "selector": "#draft_id", "id": "draft_id"},
    ]


def test_hidden_fields_listed_by_the_tool_with_selectors(page):
    page.html = HIDDEN_HTML
    asyncio.run(mcp_server._read())
    out = asyncio.run(mcp_server.zerodom_hidden_fields())
    assert out == (
        "csrf_token: abc123  (selector: input[name='csrf_token'])\n"
        "draft_id: 42  (selector: #draft_id)"
    )


def test_hidden_fields_empty_on_a_page_without_them(page):
    asyncio.run(mcp_server._read())
    out = asyncio.run(mcp_server.zerodom_hidden_fields())
    assert out == "No hidden fields on the current page."


LINK_HTML = """
<html><body>
  <a href="javascript:alert(1)" id="js">Run</a>
  <a href="data:text/html,<x>" id="dh">Raw</a>
  <a href="/normal" id="ok">Normal</a>
</body></html>
"""


def test_sidebar_payload_carries_href_for_xss_surface_flags(page):
    """F12's javascript:/data: href flags in the popup are driven by this page
    data. If the field stops flowing into the sidebar, the severity badges go
    quiet — this guards the pipeline, not just the parser."""
    page.html = LINK_HTML
    # Sidebar payload is gated on _session["attached"] and FakePage.goto
    # resets html to PAGE — stub both to keep the content we just set.
    async def keep_html(url, **kw):
        page.calls.append(("goto", url))
        page.url = url
        page.typed = {}
    page.goto = keep_html
    mcp_server._session["attached"] = True
    try:
        asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
        assert page.sidebar_eval is not None
        by_id = {n["id"]: n for n in page.sidebar_eval}
        assert by_id["01"]["href"] == "javascript:alert(1)"
        assert by_id["02"]["href"].startswith("data:text/html")
        assert by_id["03"]["href"] == "/normal"
    finally:
        mcp_server._session["attached"] = False


def test_click_in_place_returns_a_diff_not_the_whole_page(page):
    """A menu opening should cost one line, not a re-listing of every node."""
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    out = asyncio.run(mcp_server.zerodom_click_node("03"))

    assert out.splitlines()[0] == "clicked [03]"
    assert "+[04] a 'Opened'" in out
    assert "button 'Go'" not in out, "unchanged nodes must not be re-sent"
    # Ids still resolve afterwards: the server re-read even though it reported a diff.
    assert mcp_server._session["selectors"]["node_04"] == "#opened"


def test_navigation_falls_back_to_the_full_graph(page):
    """Every id renumbers across a navigation, so a diff would be noise."""
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    out = asyncio.run(mcp_server.zerodom_click_node("02"))
    assert "PAGE: Results | https://example.com/results" in out
    assert "[01] a 'Back'" in out


def test_a_click_that_changes_nothing_says_so(page):
    """Shortly after navigation, a no-op click also gets the hydration hint
    — see test_a_click_that_changes_nothing_is_plain_once_grace_expires for
    the baseline (no hint) case."""
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    asyncio.run(mcp_server.zerodom_click_node("03"))
    out = asyncio.run(mcp_server.zerodom_click_node("03"))
    assert out.splitlines()[2] == "no structural change"
    assert "handler may not have finished attaching" in out


def test_a_click_that_changes_nothing_is_plain_once_grace_expires(page):
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    mcp_server._session["navigated_at"] -= mcp_server._HYDRATION_GRACE_S + 1
    asyncio.run(mcp_server.zerodom_click_node("03"))
    out = asyncio.run(mcp_server.zerodom_click_node("03"))
    assert out.splitlines()[-1] == "no structural change"


def test_hydration_hint_suppressed_when_the_click_fired_a_request(monkeypatch, page):
    """A no-op-looking click that actually triggered a fetch/GraphQL mutation
    still pending isn't a hydration miss — it's evidence the handler *did*
    run, just hasn't resolved yet (real auth actions routinely take 800ms-2s)."""
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    asyncio.run(mcp_server.zerodom_click_node("03"))  # opens the menu -- a real diff
    tab_key = mcp_server._session["active"]
    mcp_server._session["network_log"].setdefault(tab_key, [])
    real_act = mcp_server._act

    async def act_and_fire_a_request(node_id, verb, *args):
        mcp_server._session["network_log"][tab_key].append(
            {"type": "request", "method": "POST", "url": "https://example.com/api"}
        )
        return await real_act(node_id, verb, *args)

    monkeypatch.setattr(mcp_server, "_act", act_and_fire_a_request)
    out = asyncio.run(mcp_server.zerodom_click_node("03"))  # no further DOM diff
    assert out.splitlines()[2] == "no structural change"
    assert "handler may not have finished attaching" not in out


# ─── Cross-identity replay / IDOR tools ─────────────────────────────────────

class FakeAPIResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def text(self):
        return self._body


class FakeRequestContext:
    """Stands in for a Playwright APIRequestContext: returns a canned body,
    optionally varying by the request's method, so a replay/compare is testable."""

    def __init__(self, body, status=200):
        self.body, self.status = body, status
        self.calls = []

    async def fetch(self, url, method="GET", headers=None, data=None):
        self.calls.append((method, url, data))
        return FakeAPIResponse(self.status, self.body)


@pytest.fixture
def identities():
    mcp_server._session["identities"] = {}
    yield mcp_server._session["identities"]
    mcp_server._session["identities"] = {}


def test_compare_identities_flags_identical_body_as_idor(identities):
    # Both identities get Bob's invoice → cross-tenant IDOR.
    identities["alice"] = FakeRequestContext("<h1>Invoice 2 — Bob</h1>")
    identities["bob"] = FakeRequestContext("<h1>Invoice 2 — Bob</h1>")
    out = asyncio.run(mcp_server.zerodom_compare_identities(
        "https://app/api/invoice/2", identities=["alice", "bob"]))
    assert "IDENTICAL response under alice/bob" in out
    assert "IDOR" in out


def test_compare_identities_reports_isolation_when_different(identities):
    identities["alice"] = FakeRequestContext("<h1>Alice's data</h1>")
    identities["bob"] = FakeRequestContext("<h1>Bob's data</h1>")
    out = asyncio.run(mcp_server.zerodom_compare_identities(
        "https://app/api/me", identities=["alice", "bob"]))
    assert "properly isolated" in out
    assert "IDENTICAL" not in out


def test_compare_identities_needs_two(identities):
    identities["alice"] = FakeRequestContext("x")
    out = asyncio.run(mcp_server.zerodom_compare_identities("https://app/x", identities=["alice"]))
    assert "at least two identities" in out


def test_replay_sends_through_the_named_identity(identities):
    ctx = FakeRequestContext("pong", status=201)
    identities["bob"] = ctx
    out = asyncio.run(mcp_server.zerodom_replay(
        "https://app/api/ping", method="post", body="{}", as_identity="bob"))
    assert ctx.calls == [("POST", "https://app/api/ping", "{}")]
    assert "status 201" in out and "[bob]" in out


def test_replay_rejects_unknown_identity(identities):
    with pytest.raises(ValueError, match="unknown identity"):
        asyncio.run(mcp_server.zerodom_replay("https://app/x", as_identity="ghost"))


def test_replay_refuses_undrivable_url(identities):
    with pytest.raises(ValueError, match="can't drive"):
        asyncio.run(mcp_server.zerodom_replay("chrome://settings", as_identity="live"))


# ─── Scope enforcement ──────────────────────────────────────────────────────

@pytest.fixture
def scoped():
    mcp_server._session["scope"] = None
    yield
    mcp_server._session["scope"] = None


def test_set_scope_then_out_of_scope_and_destructive_are_refused(scoped, page):
    asyncio.run(mcp_server.zerodom_set_scope("app.test,*.api.test", max_rps=None))
    # in scope is fine
    asyncio.run(mcp_server.zerodom_parse_url("https://app.test/dashboard"))
    # out of scope host refused before any navigation
    with pytest.raises(PermissionError, match="out of scope"):
        asyncio.run(mcp_server.zerodom_parse_url("https://evil.test/x"))
    # destructive URL refused even in scope
    with pytest.raises(PermissionError, match="destructive"):
        asyncio.run(mcp_server.zerodom_parse_url("https://app.test/account/delete"))


def test_destructive_click_is_refused(scoped, page):
    asyncio.run(mcp_server.zerodom_set_scope("example.com"))
    mcp_server._session["nodes"] = [
        {"id": "node_01", "type": "button", "label": "Delete account", "selector": "#del", "href": ""},
        {"id": "node_02", "type": "a", "label": "View", "selector": "#v", "href": "/view"},
    ]
    with pytest.raises(PermissionError, match="destructive"):
        asyncio.run(mcp_server._act("01", "click"))


def test_no_scope_is_unrestricted(scoped, page):
    assert mcp_server._session["scope"] is None
    # any host, any path — no scope means no enforcement (backward compatible)
    asyncio.run(mcp_server.zerodom_parse_url("https://anything.example/logout"))


def test_locked_scope_cannot_be_widened_by_the_agent(scoped):
    mcp_server._session["scope"] = {"allow": ["app.test"], "deny": mcp_server._SCOPE_DEFAULT_DENY,
                                    "max_rps": None, "locked": True, "_last": 0.0}
    out = asyncio.run(mcp_server.zerodom_set_scope("evil.test"))
    assert "locked by the operator" in out
    assert mcp_server._session["scope"]["allow"] == ["app.test"]  # unchanged
