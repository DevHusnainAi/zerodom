"""Reaching into iframes.

Each frame is a separate document with its own DOM, so a CSS selector from the
top page can never address something inside one. Playwright bridges that with
`frame_locator()`, and this module records, per node, the chain of iframe
selectors needed to get there — so `locate(page, node)` reaches any node in any
frame, and callers never have to know which is which.

Ads, trackers and consent gates are iframes too, which is why `frames=True` is
opt-in on the CLI and why frames that refuse to cooperate are skipped rather
than raised.
"""

from __future__ import annotations

from typing import Any

# `about:blank` is deliberately NOT here. A frame written by srcdoc or
# document.write keeps that URL and holds the embedded preview an agent came
# for — CodePen's result panel and every "run it" sandbox look exactly like
# this. Size and content decide whether a frame is worth reading, not its URL.
SKIP_URL_PREFIXES = ("javascript:", "data:")

# An iframe smaller than this is a tracking pixel or a consent beacon.
MIN_FRAME_PX = 40


def frame_chain(frame: Any) -> list[str] | None:
    """Selectors from the top document down to `frame`, or None if unreachable.

    Indexed rather than named: `iframe >> nth=2` survives a frame with no id,
    no name and no stable class, which is most of them.
    """
    chain: list[str] = []
    current = frame
    while current.parent_frame is not None:
        try:
            element = current.frame_element()
            index = element.evaluate(
                "e => [...e.ownerDocument.querySelectorAll('iframe')].indexOf(e)"
            )
        except Exception:
            return None
        if index < 0:
            return None
        chain.append(f"iframe >> nth={index}")
        current = current.parent_frame
    return list(reversed(chain))


async def frame_chain_async(frame: Any) -> list[str] | None:
    """`frame_chain` for async pages, nesting included.

    Playwright's two APIs share no base class, so the walk is written twice
    rather than wrapped — a sync/async shim here would be more code than the
    duplicate loop and harder to read at the point of failure.
    """
    chain: list[str] = []
    current = frame
    while current.parent_frame is not None:
        try:
            element = await current.frame_element()
            index = await element.evaluate(
                "e => [...e.ownerDocument.querySelectorAll('iframe')].indexOf(e)"
            )
        except Exception:
            return None
        if index < 0:
            return None
        chain.append(f"iframe >> nth={index}")
        current = current.parent_frame
    return list(reversed(chain))


async def is_worth_reading_async(frame: Any) -> bool:
    """`is_worth_reading` for async pages."""
    if any((frame.url or "").lower().startswith(p) for p in SKIP_URL_PREFIXES):
        return False
    try:
        element = await frame.frame_element()
        box = await element.bounding_box()
    except Exception:
        return False
    return bool(box) and box["width"] >= MIN_FRAME_PX and box["height"] >= MIN_FRAME_PX


def is_worth_reading(frame: Any) -> bool:
    """Is this frame rendered, and big enough to hold something clickable?

    `bounding_box()` returns None for a frame that is display:none or detached,
    which is most of the iframes on an ad-supported page. A visible frame below
    MIN_FRAME_PX is a tracking pixel.
    """
    if any((frame.url or "").lower().startswith(p) for p in SKIP_URL_PREFIXES):
        return False
    try:
        box = frame.frame_element().bounding_box()
    except Exception:
        return False
    return bool(box) and box["width"] >= MIN_FRAME_PX and box["height"] >= MIN_FRAME_PX


def locate(page: Any, node: dict[str, Any]) -> Any:
    """A Playwright locator for `node`, descending into its frame if it has one.

    The single place that knows how a node's `frame` chain turns into something
    clickable — click, fill and the report measurer all route through it, so
    they cannot disagree about where a node lives.
    """
    target = page
    for selector in node.get("frame") or ():
        target = target.frame_locator(selector)
    return target.locator(node["selector"])


def renumber(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-id a merged node list so ids stay dense and in document order."""
    for position, node in enumerate(nodes, 1):
        node["id"] = f"node_{position:02d}"
    return nodes
