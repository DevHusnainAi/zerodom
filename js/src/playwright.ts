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
export const SERIALIZE = `() => {
  const roots = [];
  const marked = [];
  const visit = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) { roots.push(el.shadowRoot); visit(el.shadowRoot); }
      // A stylesheet rule is invisible to a parser reading HTML text, so
      // input.hidden-class looks clickable and an agent burns a 30s timeout
      // on it. Only the browser knows; record what it knows.
      //
      // NOT contentVisibilityAuto: content-visibility:auto is a rendering
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
  }
}`;

/** Minimal surface this module needs from a Playwright Page. */
export interface PageLike extends FrameLike {
  content(): Promise<string>;
  evaluate<T>(fn: string): Promise<T>;
  frames(): FrameLike[];
  mainFrame(): FrameLike;
  frameLocator(selector: string): unknown;
  locator(selector: string): unknown;
}

/** Page HTML including open shadow roots. */
export async function serialize(page: PageLike): Promise<string> {
  return (await page.evaluate<string | null>(SERIALIZE)) ?? (await page.content());
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
   */
  async fromPage(page: PageLike, options: { frames?: boolean } = {}): Promise<InteractionGraph> {
    const html = await serialize(page);
    const graph = new ZeroDOMParser(html, (page as unknown as { url(): string }).url()).parse();
    return options.frames ? mergeFrames(page, graph) : graph;
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
async function readFrame(frame: PageLike & FrameLike): Promise<FrameNode[] | null> {
  if (!(await isWorthReading(frame))) return [];
  const chain = await frameChain(frame);
  if (chain === null) return null;
  try {
    const html = await serialize(frame as unknown as PageLike);
    const sub = new ZeroDOMParser(html, frame.url()).parse();
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
async function mergeFrames(page: PageLike, graph: InteractionGraph): Promise<InteractionGraph> {
  const children = page.frames().filter((f) => f !== page.mainFrame());
  const results = await Promise.all(
    children.map((f) => readFrame(f as PageLike & FrameLike).catch(() => null))
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
