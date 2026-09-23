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
SERIALIZE = """(opts) => {
  const { viewportOnly, checkOcclusion } = opts || {};
  const roots = [];
  const marked = [];
  const offscreenMarked = [];
  const occludedMarked = [];
  const vw = window.innerWidth, vh = window.innerHeight;
  const candidates = [];
  const visit = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.closest && (el.closest('[id^="zerodom-"]') || el.closest('[data-zerodom-ignore]'))) continue;
      if (el.shadowRoot) { roots.push(el.shadowRoot); visit(el.shadowRoot); }
      const display = getComputedStyle(el).display;
      if (display !== 'contents' && !el.checkVisibility({ visibilityProperty: true })) {
        el.setAttribute('data-zerodom-hidden', '');
        marked.push(el);
        continue;
      }
      if (!viewportOnly && !checkOcclusion) continue;
      const r = el.getBoundingClientRect();
      if (viewportOnly && (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw)) {
        el.setAttribute('data-zerodom-offscreen', '');
        offscreenMarked.push(el);
        continue;
      }
      if (r.width > 0 && r.height > 0 && (viewportOnly || checkOcclusion)) {
        candidates.push({el, r});
      }
    }
  };
  visit(document);
  // Two-pass occlusion with 3-point majority rule and pointer-events guard
  if (checkOcclusion) {
    const isIgnorable = (node) => {
      try { return getComputedStyle(node).pointerEvents === 'none'; } catch { return false; }
    };
    const isVisibleForPoint = (el, x, y) => {
      const hit = document.elementFromPoint(x, y);
      if (!hit) return false;
      let cur = hit;
      while (cur) {
        if (cur === el) return true;
        if (el.contains(cur)) return true;
        if (isIgnorable(cur)) { cur = cur.parentElement; continue; }
        if (hit !== el && !el.contains(hit) && !hit.contains(el)) {
          // hit is a different, non-ignorable element
          return false;
        }
        cur = cur.parentElement;
      }
      return true;
    };
    for (const {el, r} of candidates) {
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      if (cx < 0 || cy < 0 || cx > vw || cy > vh) continue;
      const pts = [
        [cx, cy],
        [r.left + r.width * 0.25, r.top + r.height * 0.25],
        [r.right - r.width * 0.25, r.bottom - r.height * 0.25]
      ];
      let visibleCount = 0;
      for (const [x, y] of pts) {
        if (isVisibleForPoint(el, x, y)) visibleCount++;
      }
      // Majority rule: occluded if at least 2 of 3 points fail visibility
      if (visibleCount < 2) {
        el.setAttribute('data-zerodom-occluded', '');
        occludedMarked.push(el);
      }
    }
  }
  const body = document.body;
  if (!body || !body.getHTML) {
    for (const el of marked) el.removeAttribute('data-zerodom-hidden');
    for (const el of offscreenMarked) el.removeAttribute('data-zerodom-offscreen');
    for (const el of occludedMarked) el.removeAttribute('data-zerodom-occluded');
    return null;
  }
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
    for (const el of offscreenMarked) el.removeAttribute('data-zerodom-offscreen');
    for (const el of occludedMarked) el.removeAttribute('data-zerodom-occluded');
  }
}"""


def _opts(viewport_only: bool, check_occlusion: bool) -> dict[str, bool]:
    return {"viewportOnly": viewport_only, "checkOcclusion": check_occlusion}


def serialize(page: Any, viewport_only: bool = False, check_occlusion: bool = False) -> str:
    """Page HTML including open shadow roots. Sync pages only — see `from_page`."""
    return page.evaluate(SERIALIZE, _opts(viewport_only, check_occlusion)) or page.content()


async def serialize_async(page: Any, viewport_only: bool = False, check_occlusion: bool = False) -> str:
    """`serialize` for async pages. Separate because `x or await y` would treat the
    un-awaited coroutine as truthy and never reach the fallback."""
    html = await page.evaluate(SERIALIZE, _opts(viewport_only, check_occlusion))
    return html or await page.content()


class ZeroDOM:
    """1-line Playwright integration: `ZeroDOM.from_page(page)`."""

    @staticmethod
    def from_html(html: str, url: str = "about:blank") -> InteractionGraph:
        return ZeroDOMParser(html, url).parse()

    @staticmethod
    def from_page(
        page: Any, frames: bool = False, viewport_only: bool = True, check_occlusion: bool = False, collapse_duplicates: bool = True
    ) -> InteractionGraph | Any:
        """Parse a Playwright page.

        Works with both APIs: sync pages return the graph directly, async pages
        return an awaitable that resolves to it.

        `frames=True` also walks same- and cross-origin iframes, which is the
        only way to see inside embedded editors, payment fields and consent
        gates. Off by default: it costs a serialize per frame, and on an
        ad-heavy page most of those frames are advertising.

        `viewport_only=True` drops nodes currently scrolled off-screen — cuts
        graph size substantially on long feeds (YouTube's home feed, Reddit,
        Twitter) where most rendered nodes are far below the fold. On by default
        for interactive agents.

        `check_occlusion=True` drops nodes currently covered by something else
        (a modal backdrop, an open dropdown, a cookie banner) — the exact
        situation that makes a real click throw "element intercepts pointer
        events." Off by default: an elementFromPoint() hit-test per element is
        real cost, unmeasured against this project's <50ms/5k-node budget, so
        it isn't imposed on every caller by default.

        `collapse_duplicates=True` collapses repeated low-signal controls under
        a card header with ×N. On by default for token savings.
        """
        # Duck-typed on purpose — anything with `.content()` and `.url` parses. Shadow
        # serialization also needs `.evaluate()`, and returns None when the page has no
        # shadow roots, so both paths fall back to the plain light-DOM content.
        opts = _opts(viewport_only, check_occlusion)
        pending = page.evaluate(SERIALIZE, opts) if hasattr(page, "evaluate") else None
        if inspect.isawaitable(pending):
            return _from_async_page(pending, page, frames, viewport_only, check_occlusion, collapse_duplicates)
        content = pending or page.content()
        if inspect.isawaitable(content):
            return _from_async_page(content, page, frames, viewport_only, check_occlusion, collapse_duplicates)
        graph = ZeroDOMParser(content, page.url, viewport_only, check_occlusion, collapse_duplicates).parse()
        # Hydration pending detection via framework root inspection
        if hasattr(page, "evaluate"):
            try:
                hydration = page.evaluate("""() => {
                  const hasReact = !!window.__REACT_DEVTOOLS_GLOBAL_HOOK__?.renderers;
                  const root = document.getElementById('root') || document.getElementById('__next');
                  let reactMounted = false;
                  if (root && (root.__reactFiber$ || root.__reactContainer$)) {
                    try {
                      const fiber = root.__reactFiber$ || root.__reactContainer$;
                      reactMounted = !!(fiber && fiber.child);
                    } catch(e){}
                  }
                  const hasNext = !!window.__NEXT_DATA__ || !!document.querySelector('script#__NEXT_DATA__');
                  const hasVue = !!window.__vue_app__ || !!window.__VUE_DEVTOOLS_GLOBAL_HOOK__;
                  const hasSvelte = !!window.__svelte;
                  const hydrationPending = (hasReact && root && !reactMounted) || (hasNext && !reactMounted) || hasVue || hasSvelte;
                  return {hydrationPending};
                }""")
                if hydration and hydration.get('hydrationPending'):
                    graph["metadata"]["hydration_pending"] = True
            except Exception:
                pass
        return _merge_frames(page, graph, viewport_only, check_occlusion) if frames else graph


async def _from_async_page(
    pending: Any,
    page: Any,
    frames: bool = False,
    viewport_only: bool = True,
    check_occlusion: bool = False,
    collapse_duplicates: bool = True,
) -> InteractionGraph:
    html = await pending or await page.content()
    graph = ZeroDOMParser(html, page.url, viewport_only, check_occlusion, collapse_duplicates).parse()
    try:
        hydration = await page.evaluate("""() => {
          const hasReact = !!window.__REACT_DEVTOOLS_GLOBAL_HOOK__?.renderers;
          const root = document.getElementById('root') || document.getElementById('__next');
          let reactMounted = false;
          if (root && (root.__reactFiber$ || root.__reactContainer$)) {
            try {
              const fiber = root.__reactFiber$ || root.__reactContainer$;
              reactMounted = !!(fiber && fiber.child);
            } catch(e){}
          }
          const hasNext = !!window.__NEXT_DATA__ || !!document.querySelector('script#__NEXT_DATA__');
          const hasVue = !!window.__vue_app__ || !!window.__VUE_DEVTOOLS_GLOBAL_HOOK__;
          const hasSvelte = !!window.__svelte;
          const hydrationPending = (hasReact && root && !reactMounted) || (hasNext && !reactMounted) || hasVue || hasSvelte;
          return {hydrationPending};
        }""")
        if hydration and hydration.get('hydrationPending'):
            graph["metadata"]["hydration_pending"] = True
    except Exception:
        pass
    return (
        await _merge_frames_async(page, graph, viewport_only, check_occlusion, collapse_duplicates) if frames else graph
    )


def _merge_frames(
    page: Any, graph: InteractionGraph, viewport_only: bool = False, check_occlusion: bool = False
) -> InteractionGraph:
    """Append every readable iframe's nodes to the top document's, in frame order.

    A frame that refuses to be read — detached mid-parse, or a cross-origin ad
    that blocks evaluation — is skipped and counted, never raised. One hostile
    advertisement must not cost the caller the rest of the page.
    """
    opts = _opts(viewport_only, check_occlusion)
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
            html = frame.evaluate(SERIALIZE, opts) or frame.content()
            sub = ZeroDOMParser(html, frame.url, viewport_only, check_occlusion).parse()
        except Exception:
            skipped += 1
            continue
        for node in sub["nodes"]:
            node["frame"] = chain
            node["frame_url"] = frame.url
        added += sub["nodes"]
    return _finish(graph, added, skipped)


async def _read_frame(
    frame: Any, viewport_only: bool = True, check_occlusion: bool = False, collapse_duplicates: bool = True
) -> list | None:
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
        html = await frame.evaluate(SERIALIZE, _opts(viewport_only, check_occlusion)) or await frame.content()
        sub = ZeroDOMParser(html, frame.url, viewport_only, check_occlusion, collapse_duplicates).parse()
    except Exception:
        return None
    for node in sub["nodes"]:
        node["frame"] = chain
        node["frame_url"] = frame.url
    return sub["nodes"]


async def _merge_frames_async(
    page: Any, graph: InteractionGraph, viewport_only: bool = True, check_occlusion: bool = False, collapse_duplicates: bool = True
) -> InteractionGraph:
    """`_merge_frames` for async pages, concurrently.

    Frames are independent documents, so reading them serially just adds up
    round trips. `gather` preserves order, which matters: node ids are assigned
    in document order and must stay stable between reads.
    """
    children = [f for f in page.frames if f is not page.main_frame]
    results = await asyncio.gather(
        *(_read_frame(f, viewport_only, check_occlusion, collapse_duplicates) for f in children), return_exceptions=True
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
    # ponytail: graph["metadata"]["offscreen_skipped"]/"occluded_skipped" only
    # count the top document's own skips (set inside ZeroDOMParser.parse()
    # before this runs) -- frame subgraphs' own counts aren't folded in here,
    # since `added` is already a flat node list by this point. Narrow gap:
    # only under-counts when frames=True and viewport_only/check_occlusion=True
    # together.
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
