"""Model Context Protocol server: drive a browser through the interaction graph."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from .parser import ZeroDOMParser, compact_line, find_nodes
from .playwright_wrapper import serialize_async

mcp = MCPServer("zerodom", version="0.0.1")

# ponytail: one global browser session — the MCP server drives a single agent.
# Add a session_id parameter only if concurrent pages are ever needed.
_session: dict[str, Any] = {
    "pw": None, "browser": None, "page": None,
    "selectors": {}, "nodes": [], "url": None,
}


async def _page() -> Any:
    if _session["page"] is None:
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch()
        _session.update(pw=pw, browser=browser, page=await browser.new_page())
    return _session["page"]


def _selector(node_id: str) -> str:
    """Resolve a node id to its CSS selector — the lookup map the model never sees.

    Accepts both the bare index the compact graph prints (`[03]` -> "3") and the
    full "node_03" form used in the JSON graph.
    """
    key = node_id if node_id.startswith("node_") else f"node_{node_id.strip('[]').zfill(2)}"
    selector = _session["selectors"].get(key)
    if not selector:
        raise ValueError(f"Unknown node '{node_id}'. Call zerodom_parse_url first.")
    return selector


def _identity(node: dict[str, Any]) -> tuple[str, str, str]:
    """What makes a node "the same node" across two reads. Node ids renumber on
    every parse, so they cannot be it."""
    return (node["type"], node["label"], node["selector"])


def _diff(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> str | None:
    """Only what changed, or None when a full graph would be more honest.

    An agent clicking through a page re-reads the same few hundred nodes over and
    over; on Hacker News that is ~2,350 tokens per action to say "one menu opened".
    A wholesale change (a navigation) has no useful diff, so it falls back.
    """
    old, new = {_identity(n): n for n in before}, {_identity(n): n for n in after}
    added = [n for key, n in new.items() if key not in old]
    removed = [n for key, n in old.items() if key not in new]
    changed = [
        (old[key], n)
        for key, n in new.items()
        if key in old and n.get("value") != old[key].get("value")
    ]
    if not (added or removed or changed):
        # Typing sets the DOM *property*, not the `value` attribute, so text a user
        # entered is invisible to any serializer. The fill tool echoes it instead.
        return "no structural change"
    # More than half the page moved: it is a different page, so show all of it.
    if len(added) + len(removed) > len(new) / 2:
        return None

    lines = [f"+{compact_line(n)}" for n in added]
    lines += [f"-{compact_line(n)}" for n in removed]
    lines += [
        f"~{compact_line(n)}  {was.get('value', '')!r} -> {n.get('value', '')!r}"
        for was, n in changed
    ]
    return "\n".join(lines)


async def _read(verbose: bool = False, diff: bool = False) -> str:
    """Parse the live DOM in place and refresh the selector map.

    Never navigates, so form state, scroll position and cookies survive. Every
    action re-reads through here, which is what keeps node ids pointing at the
    page the model is actually looking at.
    """
    page = await _page()
    # A click may still be navigating; content() during that raises.
    await page.wait_for_load_state("domcontentloaded")
    graph = ZeroDOMParser(await serialize_async(page), page.url).parse()
    previous, url = _session["nodes"], _session["url"]
    _session.update(
        selectors=graph.selector_map(), nodes=graph["nodes"], url=page.url
    )
    if verbose:
        return graph.to_json()
    # A navigation invalidates every id, so a diff against the old page is noise.
    if diff and previous and url == page.url and (delta := _diff(previous, graph["nodes"])):
        return delta
    return graph.to_compact_text()


@mcp.tool()
async def zerodom_parse_url(url: str, verbose: bool = False) -> str:
    """Navigate to a URL and return its interaction graph.

    Returns the compact text graph: `[03] button 'Sign In'`. CSS selectors are
    kept server-side and resolved by node id, so they never cost context — pass
    verbose=True for the full JSON including selectors.
    """
    page = await _page()
    await page.goto(url, wait_until="domcontentloaded")
    return await _read(verbose)


@mcp.tool()
async def zerodom_read_page(verbose: bool = False) -> str:
    """Re-read the current page without navigating.

    Use after an action changed the page, or when node ids look stale. Unlike
    zerodom_parse_url this does not reload, so anything typed into the page stays.
    """
    return await _read(verbose)


@mcp.tool()
async def zerodom_find(query: str) -> str:
    """Search the current page's graph for nodes matching `query`.

    Case-insensitive substring match over each node's label and type. Prefer this
    over re-reading the whole page when you already know what you are looking for:
    "checkout" costs three lines, the full graph costs every node on the page.
    """
    hits = find_nodes(_session["nodes"], query)
    if not _session["nodes"]:
        return "No page loaded. Call zerodom_parse_url first."
    if not hits:
        return f"No node matches {query!r} among {len(_session['nodes'])} nodes."
    return "\n".join(compact_line(node) for node in hits)


@mcp.tool()
async def zerodom_click_node(node_id: str) -> str:
    """Click a node and return what changed on the page.

    Returns a diff — `+` appeared, `-` gone, `~` value changed — because most
    clicks alter a handful of nodes and re-listing the page would cost hundreds.
    A navigation renumbers everything, so that returns the full graph instead.
    """
    page = await _page()
    selector = _selector(node_id)
    before = page.url
    await page.click(selector)
    graph = await _read(diff=True)
    moved = f" -> {page.url}" if page.url != before else ""
    return f"clicked [{node_id}]{moved}\n\n{graph}"


@mcp.tool()
async def zerodom_fill_node(node_id: str, text: str) -> str:
    """Type text into a node and return what changed on the page.

    The text is echoed back in the first line; the diff below it reports *structural*
    change — a validation error appearing, an autocomplete list opening.
    """
    page = await _page()
    selector = _selector(node_id)
    await page.fill(selector, text)
    return f"filled [{node_id}] with {text!r}\n\n{await _read(diff=True)}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
