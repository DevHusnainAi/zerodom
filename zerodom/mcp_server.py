"""Model Context Protocol server: drive a browser through the interaction graph."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from mcp.server import MCPServer

from . import __version__
from .frames import locate
from .parser import compact_line, find_nodes
from .playwright_wrapper import ZeroDOM

mcp = MCPServer("zerodom", version=__version__)

# ponytail: one global browser session — the MCP server drives a single agent.
# Add a session_id parameter only if concurrent pages are ever needed.
_session: dict[str, Any] = {
    "pw": None, "browser": None, "page": None, "attached": False,
    "selectors": {}, "nodes": [], "url": None, "frames": False,
}


def _cdp_endpoint() -> str | None:
    """Where to attach instead of launching, or None to launch as usual.

    ZERODOM_CDP_ENDPOINT is the sanctioned power-user/CI path: a Chrome started
    with --remote-debugging-port against a *dedicated* --user-data-dir, never the
    default profile — Chrome has refused CDP on the default profile since v136
    specifically to stop this exact session-theft vector (see docs/DECISIONS.md
    D10). The browser extension's native host (Stage 4) will populate
    ~/.zerodom/bridge.json for the same seam; that discovery path is added then,
    not speculated here.
    """
    return os.environ.get("ZERODOM_CDP_ENDPOINT") or None


async def _page() -> Any:
    if _session["page"] is None:
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        endpoint = _cdp_endpoint()
        if endpoint:
            # Fail closed: a configured-but-unreachable endpoint is an error, not
            # a reason to silently fall back to a fresh, unauthenticated browser.
            browser = await pw.chromium.connect_over_cdp(endpoint)
            page = browser.contexts[0].pages[0] if browser.contexts[0].pages else await browser.contexts[0].new_page()
            _session.update(pw=pw, browser=browser, page=page, attached=True)
        else:
            browser = await pw.chromium.launch()
            _session.update(pw=pw, browser=browser, page=await browser.new_page(), attached=False)
    return _session["page"]


def _node(node_id: str) -> dict[str, Any]:
    """Resolve a node id to the node itself — the lookup the model never sees.

    Accepts both the bare index the compact graph prints (`[03]` -> "3") and the
    full "node_03" form used in the JSON graph. Returns the whole node, not just
    its selector, because a node inside an iframe also needs its frame chain to
    be reachable.
    """
    key = node_id if node_id.startswith("node_") else f"node_{node_id.strip('[]').zfill(2)}"
    for node in _session["nodes"]:
        if node["id"] == key:
            return node
    raise ValueError(f"Unknown node '{node_id}'. Call zerodom_parse_url first.")


# Amber matches the Vexra Labs accent used elsewhere (vexralabs-com,
# zerodom's own report.py badges) — a deliberate, not arbitrary, color.
_HIGHLIGHT_JS = """(el) => {
  const r = el.getBoundingClientRect();
  const box = document.createElement('div');
  box.style.cssText = `position:fixed;left:${r.left}px;top:${r.top}px;` +
    `width:${r.width}px;height:${r.height}px;border:3px solid #e8a33d;` +
    `border-radius:4px;box-shadow:0 0 0 4px rgba(232,163,61,0.35);` +
    `pointer-events:none;z-index:2147483647;transition:opacity .25s ease;`;
  document.body.appendChild(box);
  setTimeout(() => { box.style.opacity = '0'; }, 250);
  setTimeout(() => box.remove(), 600);
}"""

_HIGHLIGHT_PAUSE_S = 0.35


async def _highlight(locator: Any) -> None:
    """A brief pulse over the real element before acting on it — the one thing
    actually visible in an attached session, since chrome.debugger itself never
    opens DevTools or resizes anything (docs/DECISIONS.md D11). Only meaningful
    when a person is watching a real tab, so it's skipped entirely for launched
    (headless, unwatched) sessions — see the `attached` check in `_act()`.

    Cosmetic only: a failure here must never block the real action underneath
    it, so exceptions are swallowed rather than propagated.
    """
    try:
        await locator.evaluate(_HIGHLIGHT_JS)
        await asyncio.sleep(_HIGHLIGHT_PAUSE_S)
    except Exception:
        pass


# A small floating transcript, not a growing wall — panel.children[0] is the
# title, so the cap keeps at most 6 log lines plus it. Page-level (page.evaluate,
# not locator.evaluate) because the panel isn't tied to whichever element was
# just acted on, and lives on the top document even when the action happened
# inside a frame — an agent's own activity log belongs with the viewer, not
# buried inside whatever iframe the click happened to be in.
_LOG_JS = """(text) => {
  let panel = document.getElementById('zerodom-log-panel');
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'zerodom-log-panel';
    panel.style.cssText = 'position:fixed;bottom:16px;right:16px;max-width:320px;' +
      'max-height:200px;overflow-y:auto;background:rgba(23,24,27,.92);' +
      'color:#e8eaed;font:12px/1.5 ui-monospace,Menlo,monospace;padding:10px 12px;' +
      'border-radius:8px;border:1px solid rgba(232,163,61,.4);' +
      'box-shadow:0 8px 24px rgba(0,0,0,.35);pointer-events:none;z-index:2147483647;';
    const title = document.createElement('div');
    title.textContent = 'zerodom';
    title.style.cssText = 'color:#e8a33d;font-weight:600;margin-bottom:4px;';
    panel.appendChild(title);
    document.body.appendChild(panel);
  }
  const line = document.createElement('div');
  line.textContent = text;
  line.style.cssText = 'opacity:0;transition:opacity .2s ease;white-space:nowrap;' +
    'overflow:hidden;text-overflow:ellipsis;';
  panel.appendChild(line);
  requestAnimationFrame(() => { line.style.opacity = '1'; });
  while (panel.children.length > 7) panel.removeChild(panel.children[1]);
}"""


async def _log_action(page: Any, text: str) -> None:
    """Append one line to the on-page activity panel — the audit-trail piece
    Stage 5b exists for (ROADMAP.md). Same reasoning as `_highlight`: gated to
    attached sessions in `_act()`, cosmetic only, never allowed to block the
    real action it's describing.
    """
    try:
        await page.evaluate(_LOG_JS, text)
    except Exception:
        pass


async def _act(node_id: str, verb: str, *args: Any) -> str:
    """Click or fill a node, retrying once if the browser drops underneath us.

    Chromium crashed on exactly one of fifty benchmarked sites. A crash kills the
    page but not the session, so a bare retry on a fresh page recovers the run
    instead of ending it.
    """
    node = _node(node_id)
    for attempt in (1, 2):
        page = await _page()
        try:
            locator = locate(page, node)
            if _session["attached"]:
                await _highlight(locator)
            await getattr(locator, verb)(*args)
            if _session["attached"]:
                verb_past = {"click": "clicked", "fill": "filled"}.get(verb, verb)
                detail = f" = {args[0]!r}" if args else ""
                await _log_action(page, f"{verb_past} {compact_line(node)}{detail}")
            return ""
        except Exception as exc:
            if attempt == 2 or not _is_crash(exc):
                raise
            await _restart(page.url)
            node = _node(node_id)
    return ""


def _is_crash(exc: Exception) -> bool:
    text = str(exc).lower()
    return "crash" in text or "target closed" in text or "browser has been closed" in text


async def _restart(url: str) -> None:
    """Rebuild the browser after a crash and return to where we were.

    An attached session (Stage 0+) didn't launch the browser — it's the user's
    real, already-open Chrome. Closing it here would take their browser down on
    every crash-recovery, not just detach from it, so only pw.stop() runs and the
    page/browser handles are dropped without .close() for that case; _page()
    reconnects to the still-live endpoint on the next call.
    """
    attached = _session.get("attached")
    for key in ("page", "browser", "pw"):
        obj = _session.get(key)
        if obj is not None and not (attached and key in ("page", "browser")):
            try:
                await (obj.stop() if key == "pw" else obj.close())
            except Exception:
                pass
        _session[key] = None
    _session["attached"] = False
    page = await _page()
    if url and url != "about:blank":
        await page.goto(url, wait_until="domcontentloaded")
    await _read()


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
    graph = await ZeroDOM.from_page(page, frames=_session["frames"])
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
async def zerodom_parse_url(url: str, verbose: bool = False, frames: bool = False) -> str:
    """Navigate to a URL and return its interaction graph.

    Returns the compact text graph: `[03] button 'Sign In'`. CSS selectors are
    kept server-side and resolved by node id, so they never cost context — pass
    verbose=True for the full JSON including selectors.

    Set frames=True when the controls you need are inside an iframe — embedded
    editors, payment fields, consent gates. Off by default because it costs a
    read per frame and most frames on a commercial page are advertising.
    """
    _session["frames"] = frames
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
    before = page.url
    await _act(node_id, "click")
    page = await _page()
    graph = await _read(diff=True)
    moved = f" -> {page.url}" if page.url != before else ""
    return f"clicked [{node_id}]{moved}\n\n{graph}"


@mcp.tool()
async def zerodom_fill_node(node_id: str, text: str) -> str:
    """Type text into a node and return what changed on the page.

    The text is echoed back in the first line; the diff below it reports *structural*
    change — a validation error appearing, an autocomplete list opening.
    """
    await _act(node_id, "fill", text)
    return f"filled [{node_id}] with {text!r}\n\n{await _read(diff=True)}"


@mcp.tool()
async def zerodom_scroll(direction: str = "down", amount: int = 800) -> str:
    """Scroll the page and return what's newly visible.

    direction: "up" or "down". amount: pixels, roughly one screenful is 800.
    Dispatches a real wheel event at the viewport center rather than
    `window.scrollBy`, so it scrolls whatever scrollable container is actually
    under the cursor — a nested feed/sidebar, not just the document body,
    matching what a real scroll gesture would do.
    """
    page = await _page()
    delta = amount if direction == "down" else -amount
    await page.mouse.wheel(0, delta)
    if _session["attached"]:
        await _log_action(page, f"scrolled {direction} {amount}px")
    return await _read(diff=True)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
