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
from .parser import InteractionGraph, ZeroDOMParser, diff_nodes

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


# Wait for the DOM to stop moving before we serialize it. The parser is
# deterministic on fixed HTML, but a live page mid-render is a moving target:
# nodes get injected, labels get filled in, panels hydrate milliseconds after
# load — so two reads taken an instant apart can legitimately differ. This
# bounded settle makes the read land on a quiet moment instead of a half-built
# one. It resolves as soon as the DOM has been quiet for `quietMs`, or hits a
# hard `timeoutMs` cap.
#
# ponytail: observes childList/subtree/characterData only, NOT attributes — so
# a CSS-animated carousel, a spinner or a class-toggling banner (pure attribute
# churn) still lets the page settle; only real structural/text changes reset the
# quiet timer. A page that truly never stops adding nodes or retyping text hits
# the cap and reads as-is — honest, bounded. Add `attributes:true` if a caller
# ever needs attribute-driven churn caught.
SETTLE = """(o) => new Promise((resolve) => {
  let t, done = false;
  const fin = () => { if (done) return; done = true; clearTimeout(t); clearTimeout(hard); obs.disconnect(); resolve(); };
  const obs = new MutationObserver(() => { clearTimeout(t); t = setTimeout(fin, o.quietMs); });
  let hard;
  try { obs.observe(document.documentElement, { childList: true, subtree: true, characterData: true }); }
  catch (e) { return resolve(); }
  hard = setTimeout(fin, o.timeoutMs);
  t = setTimeout(fin, o.quietMs);
})"""

# Quiet window / hard cap in ms. A settled page costs ~quietMs; a churning one
# is bounded by timeoutMs. Both are wall-clock waits, not parse time.
_SETTLE_OPTS = {"quietMs": 200, "timeoutMs": 1200}


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
        page: Any, frames: bool = False, viewport_only: bool = True, check_occlusion: bool = False, collapse_duplicates: bool = True, settle: bool = True
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

        `settle=True` waits for async DOM churn to quiesce before serializing
        (see SETTLE above) so the read lands on a stable moment, not a page
        still injecting nodes or filling labels. On by default; set False for a
        latency-critical read where you want the DOM exactly as of now.
        """
        # Duck-typed on purpose — anything with `.content()` and `.url` parses. Shadow
        # serialization also needs `.evaluate()`, and returns None when the page has no
        # shadow roots, so both paths fall back to the plain light-DOM content.
        opts = _opts(viewport_only, check_occlusion)
        if settle and hasattr(page, "evaluate"):
            # Settle first; it also doubles as the sync/async probe. An awaitable
            # here means an async page, so hand the whole read to the async path.
            # A navigation mid-settle destroys the execution context and raises —
            # that just means the page moved on, so swallow it and serialize the
            # page we land on (Playwright's next evaluate waits for the new frame).
            try:
                settled = page.evaluate(SETTLE, _SETTLE_OPTS)
            except Exception:
                settled = None
            if inspect.isawaitable(settled):
                return _from_async_page_settled(settled, page, frames, viewport_only, check_occlusion, collapse_duplicates)
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

    @staticmethod
    def agent_snapshot(page: Any, **kwargs: Any) -> tuple[str, dict[str, str]] | Any:
        """Full-page interaction snapshot for an action agent.

        Returns `(compact_text, ref_map)`: the token-dense `[NN]`-ref listing to
        put in the model's context (the `terse` variant — no `a` tag or label quotes,
        since the agent acts by ref), and a `{ref: selector}` map keyed to those same
        refs so the agent can act on what it read — `[03]` -> selector -> click/fill.

        Defaults to `viewport_only=False`: an agent can scroll, so it needs the whole
        interactive surface, not just what is on screen (the `from_page` default is
        the on-screen view, right for a human observer, wrong for an agent). Async
        pages return an awaitable, mirroring `from_page`.
        """
        kwargs.setdefault("viewport_only", False)
        graph = ZeroDOM.from_page(page, **kwargs)
        if inspect.isawaitable(graph):
            async def _await() -> tuple[str, dict[str, str]]:
                g = await graph
                return g.to_compact_text(terse=True), g.ref_map()
            return _await()
        return graph.to_compact_text(terse=True), graph.ref_map()

    @staticmethod
    def agent_snapshot_delta(
        page: Any, since: dict[str, Any] | None = None, **kwargs: Any
    ) -> tuple[str, dict[str, str], dict[str, Any]] | Any:
        """Delta agent snapshot for a multi-turn loop — the per-turn cost lever.

        An agent re-snapshots after every action, but most turns change little. The
        first call (`since=None`) returns a full terse snapshot; later calls return
        only what changed (`+`/`-`/`~` lines keyed by selector, so refs stay valid),
        which is a fraction of the tokens. Returns `(text, ref_map, state)` — put
        `text` in context, resolve clicks via `ref_map`, and pass `state` back as
        `since` next turn. Async pages return an awaitable, mirroring `from_page`.
        """
        kwargs.setdefault("viewport_only", False)

        def _render(graph: InteractionGraph) -> tuple[str, dict[str, str], dict[str, Any]]:
            text, ref_map, state = diff_nodes(graph["nodes"], since)
            if since is None:  # prepend the page header on the first (full) snapshot
                meta = graph["metadata"]
                text = f"PAGE: {meta['page_title']} | {meta['url']}\n{text}"
            return text, ref_map, state

        graph = ZeroDOM.from_page(page, **kwargs)
        if inspect.isawaitable(graph):
            async def _await() -> tuple[str, dict[str, str], dict[str, Any]]:
                return _render(await graph)
            return _await()
        return _render(graph)


async def _from_async_page_settled(
    settled: Any,
    page: Any,
    frames: bool = False,
    viewport_only: bool = True,
    check_occlusion: bool = False,
    collapse_duplicates: bool = True,
) -> InteractionGraph:
    """Await the DOM-settle promise, then run the normal async read."""
    try:
        await settled
    except Exception:
        pass  # a page with no document, or that refused the observer, just reads as-is
    pending = page.evaluate(SERIALIZE, _opts(viewport_only, check_occlusion))
    return await _from_async_page(pending, page, frames, viewport_only, check_occlusion, collapse_duplicates)


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
