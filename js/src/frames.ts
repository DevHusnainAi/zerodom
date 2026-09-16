/**
 * Reaching into iframes. Port of zerodom/frames.py.
 *
 * Each frame is a separate document with its own DOM, so a CSS selector from the
 * top page can never address something inside one. Playwright bridges that with
 * `frameLocator()`, and this module records, per node, the chain of iframe
 * selectors needed to get there — so `locate(page, node)` reaches any node in any
 * frame, and callers never have to know which is which.
 *
 * Node's Playwright API is async-only (no sync/async split like Python's), so
 * there's one version of each function here where the Python source has two.
 */

import type { GraphNode } from "./parser.js";

/** `about:blank` is deliberately NOT here — a frame written by srcdoc or
 * document.write keeps that URL and holds the embedded preview an agent came
 * for. Size and content decide whether a frame is worth reading, not its URL. */
const SKIP_URL_PREFIXES = ["javascript:", "data:"];

// An iframe smaller than this is a tracking pixel or a consent beacon.
const MIN_FRAME_PX = 40;

/** Minimal surface this module needs from a Playwright Frame. */
export interface FrameLike {
  url(): string;
  parentFrame(): FrameLike | null;
  frameElement(): Promise<{ evaluate(fn: string): Promise<number>; boundingBox(): Promise<{ width: number; height: number } | null> }>;
}

/**
 * Selectors from the top document down to `frame`, or null if unreachable.
 *
 * Indexed rather than named: `iframe >> nth=2` survives a frame with no id, no
 * name and no stable class, which is most of them.
 */
export async function frameChain(frame: FrameLike): Promise<string[] | null> {
  const chain: string[] = [];
  let current: FrameLike = frame;
  let parent = current.parentFrame();
  while (parent !== null) {
    let index: number;
    try {
      const element = await current.frameElement();
      index = await element.evaluate("e => [...e.ownerDocument.querySelectorAll('iframe')].indexOf(e)");
    } catch {
      return null;
    }
    if (index < 0) return null;
    chain.push(`iframe >> nth=${index}`);
    current = parent;
    parent = current.parentFrame();
  }
  return chain.reverse();
}

/**
 * Is this frame rendered, and big enough to hold something clickable?
 *
 * `boundingBox()` returns null for a frame that is display:none or detached,
 * which is most of the iframes on an ad-supported page. A visible frame below
 * MIN_FRAME_PX is a tracking pixel.
 */
export async function isWorthReading(frame: FrameLike): Promise<boolean> {
  if (SKIP_URL_PREFIXES.some((p) => (frame.url() || "").toLowerCase().startsWith(p))) return false;
  try {
    const element = await frame.frameElement();
    const box = await element.boundingBox();
    return !!box && box.width >= MIN_FRAME_PX && box.height >= MIN_FRAME_PX;
  } catch {
    return false;
  }
}

/** Minimal surface this module needs from a Playwright Page or FrameLocator. */
export interface LocatableLike {
  frameLocator(selector: string): LocatableLike;
  locator(selector: string): unknown;
}

/**
 * A Playwright locator for `node`, descending into its frame if it has one.
 *
 * The single place that knows how a node's `frame` chain turns into something
 * clickable — click, fill and the report measurer all route through it, so they
 * cannot disagree about where a node lives.
 */
export function locate(page: LocatableLike, node: GraphNode): unknown {
  let target = page;
  for (const selector of node.frame ?? []) target = target.frameLocator(selector);
  return target.locator(node.selector);
}

/** Re-id a merged node list so ids stay dense and in document order. */
export function renumber(nodes: GraphNode[]): GraphNode[] {
  nodes.forEach((node, i) => {
    node.id = `node_${String(i + 1).padStart(2, "0")}`;
  });
  return nodes;
}
