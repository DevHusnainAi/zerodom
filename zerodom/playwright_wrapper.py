"""Playwright middleware adapter."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from .frames import (
    frame_chain,
    frame_chain_async,
    is_worth_reading,
    is_worth_reading_async,
    locate,
    renumber,
)
from .parser import InteractionGraph, ZeroDOMParser

__all__ = ["ZeroDOM", "serialize", "serialize_async", "locate", "SERIALIZE"]

# `page.content()` serializes light DOM only, so controls inside a shadow root are
# invisible — and worse, Playwright's CSS engine *pierces* open shadow roots when it
# clicks, so a light-DOM path like `#host > button` silently matches shadow elements
# we never knew were there. Chromium's getHTML() emits open roots as
# `<template shadowrootmode>`, which gives the parser the whole picture.
#
# Closed roots stay unreachable by design; no API exposes them.
SERIALIZE = """() => {
  const roots = [];
  const marked = [];
  const visit = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) { roots.push(el.shadowRoot); visit(el.shadowRoot); }
      // A stylesheet rule is invisible to a parser reading HTML text, so
      // `<input class="hidden">` looks clickable and an agent burns a 30s
      // timeout on it. Only the browser knows; record what it knows.
      //
      // NOT contentVisibilityAuto: `content-visibility: auto` is a rendering
      // optimisation for offscreen content, not a way to hide it. Counting it
      // as hidden deletes everything below the fold — on vercel.com that was
      // 129 of 177 controls, an agent blinded to most of the page.
      if (!el.checkVisibility({ visibilityProperty: true })) {
        el.setAttribute('data-zerodom-hidden', '');
        marked.push(el);
      }
    }
  };
  visit(document);
  const body = document.body;
  if (!body || !body.getHTML) { for (const el of marked) el.removeAttribute('data-zerodom-hidden'); return null; }
  try {
    // Only <body>, never <head>. Scripts routinely append a <div> into head;
    // Chromium serializes it faithfully, but re-parsing HTML text treats flow
    // content in head as the implicit start of body — which relocated 40+ head
    // elements into europa.eu's body and shifted every nth-of-type index after
    // them. The title is the one thing worth carrying across.
    const attrs = (el) => [...el.attributes].map(a => ` ${a.name}="${a.value.replace(/"/g, '&quot;')}"`).join('');
    const title = document.title.replace(/&/g, '&amp;').replace(/</g, '&lt;');
    return '<html' + attrs(document.documentElement) + '><head><title>' + title
         + '</title></head><body' + attrs(body) + '>'
         + body.getHTML({ serializableShadowRoots: true, shadowRoots: roots })
         + '</body></html>';
  } finally {
    // The page belongs to the caller; leave it exactly as we found it. This
    // whole function is synchronous, so nothing else can observe the marks.
    for (const el of marked) el.removeAttribute('data-zerodom-hidden');
  }
}"""


def serialize(page: Any) -> str:
    """Page HTML including open shadow roots. Sync pages only — see `from_page`."""
    return page.evaluate(SERIALIZE) or page.content()


async def serialize_async(page: Any) -> str:
    """`serialize` for async pages. Separate because `x or await y` would treat the
    un-awaited coroutine as truthy and never reach the fallback."""
    return await page.evaluate(SERIALIZE) or await page.content()


class ZeroDOM:
    """1-line Playwright integration: `ZeroDOM.from_page(page)`."""

    @staticmethod
    def from_html(html: str, url: str = "about:blank") -> InteractionGraph:
        return ZeroDOMParser(html, url).parse()

    @staticmethod
    def from_page(page: Any, frames: bool = False) -> InteractionGraph | Any:
        """Parse a Playwright page.

        Works with both APIs: sync pages return the graph directly, async pages
        return an awaitable that resolves to it.

        `frames=True` also walks same- and cross-origin iframes, which is the
        only way to see inside embedded editors, payment fields and consent
        gates. Off by default: it costs a serialize per frame, and on an
        ad-heavy page most of those frames are advertising.
        """
        # Duck-typed on purpose — anything with `.content()` and `.url` parses. Shadow
        # serialization also needs `.evaluate()`, and returns None when the page has no
        # shadow roots, so both paths fall back to the plain light-DOM content.
        pending = page.evaluate(SERIALIZE) if hasattr(page, "evaluate") else None
        if inspect.isawaitable(pending):
            return _from_async_page(pending, page, frames)
        content = pending or page.content()
        if inspect.isawaitable(content):
            return _from_async_page(content, page, frames)
        graph = ZeroDOMParser(content, page.url).parse()
        return _merge_frames(page, graph) if frames else graph


async def _from_async_page(pending: Any, page: Any, frames: bool = False) -> InteractionGraph:
    html = await pending or await page.content()
    graph = ZeroDOMParser(html, page.url).parse()
    return await _merge_frames_async(page, graph) if frames else graph


def _merge_frames(page: Any, graph: InteractionGraph) -> InteractionGraph:
    """Append every readable iframe's nodes to the top document's, in frame order.

    A frame that refuses to be read — detached mid-parse, or a cross-origin ad
    that blocks evaluation — is skipped and counted, never raised. One hostile
    advertisement must not cost the caller the rest of the page.
    """
    added, skipped = [], 0
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        if not is_worth_reading(frame):
            continue
        chain = frame_chain(frame)
        if chain is None:
            skipped += 1
            continue
        try:
            html = frame.evaluate(SERIALIZE) or frame.content()
            sub = ZeroDOMParser(html, frame.url).parse()
        except Exception:
            skipped += 1
            continue
        for node in sub["nodes"]:
            node["frame"] = chain
            node["frame_url"] = frame.url
        added += sub["nodes"]
    return _finish(graph, added, skipped)


async def _read_frame(frame: Any) -> list | None:
    """One frame's nodes, `[]` if not worth reading, `None` if it refused.

    Every failure is swallowed here so that `gather` below never has to care:
    one hostile advertisement must not cost the caller the rest of the page.
    """
    if not await is_worth_reading_async(frame):
        return []
    chain = await frame_chain_async(frame)
    if chain is None:
        return None
    try:
        html = await frame.evaluate(SERIALIZE) or await frame.content()
        sub = ZeroDOMParser(html, frame.url).parse()
    except Exception:
        return None
    for node in sub["nodes"]:
        node["frame"] = chain
        node["frame_url"] = frame.url
    return sub["nodes"]


async def _merge_frames_async(page: Any, graph: InteractionGraph) -> InteractionGraph:
    """`_merge_frames` for async pages, concurrently.

    Frames are independent documents, so reading them serially just adds up
    round trips. `gather` preserves order, which matters: node ids are assigned
    in document order and must stay stable between reads.
    """
    children = [f for f in page.frames if f is not page.main_frame]
    results = await asyncio.gather(
        *(_read_frame(f) for f in children), return_exceptions=True
    )
    added: list = []
    skipped = 0
    for result in results:
        if isinstance(result, BaseException) or result is None:
            skipped += 1
        else:
            added += result
    return _finish(graph, added, skipped)


def _finish(graph: InteractionGraph, added: list, skipped: int) -> InteractionGraph:
    if added:
        graph["nodes"] = renumber(graph["nodes"] + added)
        graph["metadata"]["total_interactive_nodes"] = len(graph["nodes"])
        # A graph that reached into frames is no longer "almost empty because
        # the content is in an iframe" — that warning would now be misleading.
        graph["metadata"].pop("warning", None)
    graph["metadata"]["frames_read"] = len({tuple(n["frame"]) for n in added})
    if skipped:
        graph["metadata"]["frames_skipped"] = skipped
    return graph
