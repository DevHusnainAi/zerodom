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


class FakePage:
    """Stands in for a Playwright page so MCP tools are testable without a browser."""

    def __init__(self):
        self.calls = []
        self.url = "https://example.com/"
        self.html = PAGE
        self.typed: dict[str, str] = {}

    async def goto(self, url, **kw):
        self.calls.append(("goto", url))
        self.url, self.html, self.typed = url, PAGE, {}

    async def content(self):
        return self.html

    async def evaluate(self, script):
        return None  # no shadow roots, so the serializer falls back to content()

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
    mcp_server._session.update(page=fake, selectors={}, nodes=[], url=None)
    yield fake
    mcp_server._session.update(page=None, selectors={}, nodes=[], url=None)


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


def test_find_before_any_page_is_loaded(page):
    assert "No page loaded" in asyncio.run(mcp_server.zerodom_find("x"))


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
    asyncio.run(mcp_server.zerodom_parse_url("https://example.com/"))
    asyncio.run(mcp_server.zerodom_click_node("03"))
    out = asyncio.run(mcp_server.zerodom_click_node("03"))
    assert out.splitlines()[-1] == "no structural change"
