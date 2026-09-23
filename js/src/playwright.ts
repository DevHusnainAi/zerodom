/** Playwright (Node) middleware adapter. Port of zerodom/playwright_wrapper.py. */

import type { FrameLike } from "./frames.js";
import { frameChain, isWorthReading, renumber } from "./frames.js";
import type { GraphMetadata } from "./parser.js";
import { InteractionGraph, ZeroDOMParser } from "./parser.js";

// `page.content()` serializes light DOM only, so controls inside a shadow root are
// invisible — and worse, Playwright's CSS engine *pierces* open shadow roots when it
// clicks, so a light-DOM path like `#host > button` silently matches shadow elements
// we never knew were there. Chromium's getHTML() emits open roots as
// `<template shadowrootmode>`, which gives the parser the whole picture.
//
// Closed roots stay unreachable by design; no API exposes them. Identical to the
// Python SERIALIZE string — it's JavaScript either way, evaluated in the page.
export const SERIALIZE = `(opts) => {
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
      if (!el.checkVisibility({ visibilityProperty: true })) {
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
        if (hit !== el && !el.contains(hit) && !hit.contains(el)) return false;
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
    const attrs = (el) => [...el.attributes].map(a => \` \${a.name}="\${a.value.replace(/"/g, '&quot;')}"\`).join('');
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
}`;

/** Minimal surface this module needs from a Playwright Page. */
export interface PageLike extends FrameLike {
  content(): Promise<string>;
  evaluate<T>(fn: string, arg?: unknown): Promise<T>;
  frames(): FrameLike[];
  mainFrame(): FrameLike;
  frameLocator(selector: string): unknown;
  locator(selector: string): unknown;
}

function serializeOpts(viewportOnly: boolean, checkOcclusion: boolean) {
  return { viewportOnly, checkOcclusion };
}

/** Page HTML including open shadow roots. */
export async function serialize(
  page: PageLike, viewportOnly = true, checkOcclusion = false
): Promise<string> {
  const html = await page.evaluate<string | null>(SERIALIZE, serializeOpts(viewportOnly, checkOcclusion));
  return html ?? (await page.content());
}

/** 1-line Playwright integration: `ZeroDOM.fromPage(page)`. */
export const ZeroDOM = {
  fromHtml(html: string, url = "about:blank"): InteractionGraph {
    return new ZeroDOMParser(html, url).parse();
  },

  /**
   * Parse a Playwright page.
   *
   * `frames: true` also walks same- and cross-origin iframes, which is the only
   * way to see inside embedded editors, payment fields and consent gates. Off by
   * default: it costs a serialize per frame, and on an ad-heavy page most of
   * those frames are advertising.
   *
   * `viewportOnly: true` drops nodes currently scrolled off-screen — cuts graph
   * size substantially on long feeds where most rendered nodes are far below
   * the fold. Off by default: it costs a getBoundingClientRect() per element.
   *
   * `checkOcclusion: true` drops nodes currently covered by something else (a
   * modal backdrop, an open dropdown, a cookie banner) — the exact situation
   * that makes a real click throw "element intercepts pointer events." Off by
   * default: an elementFromPoint() hit-test per element is real, unmeasured
   * cost.
   */
  async fromPage(
    page: PageLike,
    options: { frames?: boolean; viewportOnly?: boolean; checkOcclusion?: boolean } = {}
  ): Promise<InteractionGraph> {
    const viewportOnly = options.viewportOnly ?? true;
    const checkOcclusion = options.checkOcclusion ?? false;
    const html = await serialize(page, viewportOnly, checkOcclusion);
    const graph = new ZeroDOMParser(
      html, (page as unknown as { url(): string }).url(), viewportOnly, checkOcclusion, true
    ).parse();
    try {
      const hydration = await page.evaluate(`() => {
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
      }`) as {hydrationPending: boolean};
      if (hydration?.hydrationPending) {
        (graph.metadata as any).hydration_pending = true;
      }
    } catch {}
    return options.frames ? mergeFrames(page, graph, viewportOnly, checkOcclusion) : graph;
  },
};

interface FrameNode {
  frame?: string[];
  frame_url?: string;
  [key: string]: unknown;
}

/**
 * One frame's nodes, `[]` if not worth reading, `null` if it refused.
 *
 * Every failure is swallowed here so `Promise.all` never has to care: one
 * hostile advertisement must not cost the caller the rest of the page.
 */
async function readFrame(
  frame: PageLike & FrameLike, viewportOnly = true, checkOcclusion = false
): Promise<FrameNode[] | null> {
  if (!(await isWorthReading(frame))) return [];
  const chain = await frameChain(frame);
  if (chain === null) return null;
  try {
    const html = await serialize(frame as unknown as PageLike, viewportOnly, checkOcclusion);
    const sub = new ZeroDOMParser(html, frame.url(), viewportOnly, checkOcclusion, true).parse();
    for (const node of sub.nodes as unknown as FrameNode[]) {
      node.frame = chain;
      node.frame_url = frame.url();
    }
    return sub.nodes as unknown as FrameNode[];
  } catch {
    return null;
  }
}

/**
 * Append every readable iframe's nodes to the top document's, in frame order.
 *
 * Frames are independent documents, so reading them serially just adds up round
 * trips. `Promise.all` preserves order, which matters: node ids are assigned in
 * document order and must stay stable between reads.
 */
async function mergeFrames(
  page: PageLike, graph: InteractionGraph, viewportOnly = true, checkOcclusion = false
): Promise<InteractionGraph> {
  const children = page.frames().filter((f) => f !== page.mainFrame());
  const results = await Promise.all(
    children.map((f) =>
      readFrame(f as PageLike & FrameLike, viewportOnly, checkOcclusion).catch(() => null)
    )
  );

  const added: FrameNode[] = [];
  let skipped = 0;
  for (const result of results) {
    if (result === null) skipped += 1;
    else added.push(...result);
  }

  if (added.length) {
    graph.nodes = renumber([...graph.nodes, ...(added as unknown as typeof graph.nodes)]);
    graph.metadata.total_interactive_nodes = graph.nodes.length;
    // A graph that reached into frames is no longer "almost empty because the
    // content is in an iframe" — that warning would now be misleading.
    delete (graph.metadata as GraphMetadata).warning;
  }
  graph.metadata.frames_read = new Set(added.map((n) => JSON.stringify(n.frame))).size;
  if (skipped) graph.metadata.frames_skipped = skipped;
  return graph;
}
