/**
 * Core DOM-to-Interaction-Graph engine.
 *
 * Built on linkedom rather than jsdom: it implements the same DOM API (querySelector,
 * getElementById, standard traversal) with a much smaller footprint. Like lxml on the
 * Python side, it treats `<template>` as an ordinary element for traversal purposes
 * rather than splitting its children into a separate content fragment — verified
 * empirically, since that's exactly the behavior the shadow-DOM logic below depends on.
 * Direct port of zerodom/parser.py.
 */

import { parseHTML } from "linkedom";

import { LabelLinker, textOf } from "./labelLinker.js";
export { LabelLinker, textOf, getLabel } from "./labelLinker.js";

// `head` is deliberately absent: everything bloated inside it is pruned by name
// anyway, and a parser that mis-files stray body content into head would lose those
// nodes if head were skipped wholesale.
const PRUNE_TAGS = new Set(["script", "style", "link", "svg", "meta", "noscript", "template"]);

/**
 * A declarative shadow root: `<template shadowrootmode="open">`.
 *
 * Inert `<template>` content is pruned, but this one is a live subtree the browser
 * renders — Chromium serializes open shadow roots back into exactly this form.
 */
export function isShadowRoot(el: Element): boolean {
  return el.localName === "template" && el.hasAttribute("shadowrootmode");
}

const INTERACTIVE_TAGS = new Set(["a", "button", "input", "select", "textarea"]);
const INTERACTIVE_ROLES = new Set([
  "button", "link", "textbox", "checkbox", "radio", "combobox",
  "searchbox", "switch", "menuitem", "tab", "option", "slider",
]);

// input[type] -> ARIA role. Anything unlisted (text, email, password, ...) is a textbox.
const INPUT_ROLES: Record<string, string> = {
  checkbox: "checkbox",
  radio: "radio",
  submit: "button",
  button: "button",
  reset: "button",
  image: "button",
  file: "button", // ARIA: opens a chooser — clickable, not fillable
  search: "searchbox",
  range: "slider",
};
const TAG_ROLES: Record<string, string> = { a: "link", button: "button", select: "combobox", textarea: "textbox" };

// Roles driven by typing text rather than clicking.
const FILLABLE_ROLES = new Set(["textbox", "searchbox", "slider"]);

// A "card" is the nearest ancestor that groups one repeated list/feed item —
// used to disambiguate identically-labeled controls (twenty "Upvote" buttons
// on a feed) by grouping each under its own item's title in toCompactText().
// Semantic tags/roles first; isStructuralCard below is the fallback for
// component-library markup with neither — see the matching comment in
// parser.py for the full reasoning, including why <form> is excluded.
const CARD_TAGS = new Set(["article", "li", "tr"]);
const CARD_ROLES = new Set(["article", "listitem", "row"]);

// A card title is pure repeated overhead (one extra line per card, not per
// node), so it gets a tighter cap than a regular node label's MAX_LABEL_LEN
// equivalent in labelLinker.ts.
const CARD_TITLE_MAX_LEN = 40;

function clampCardTitle(text: string): string {
  if (!text || text.length <= CARD_TITLE_MAX_LEN) return text;
  return text.slice(0, CARD_TITLE_MAX_LEN - 1).trimEnd() + "…";
}

// Sentinel so the very first node (whose card may legitimately be undefined)
// always runs the "did the card change" check in toCompactText().
const NO_CARD = Symbol("no-card");

// A CSS identifier cannot start with a digit, so `#49151933` is a parse error, not a
// miss — querySelector throws on it. Hacker News numbers every row that way.
const CSS_IDENT = /^-?[_a-zA-Z][_a-zA-Z0-9-]*$/;

const DOCUMENT_START = /^\s*(<!doctype\s+html|<html[\s>])/i;

/**
 * Unlike lxml.html.document_fromstring (or a real browser), linkedom's parser does
 * not normalize a bare fragment into a full document — it takes the first top-level
 * tag as `documentElement` and silently drops any siblings after it. `page.content()`
 * always returns a full document, so this only matters for hand-fed HTML, but it has
 * to be handled once, here, rather than requiring every caller to know about it.
 */
function normalizeDocument(html: string): string {
  const trimmed = html.trim();
  if (!trimmed) return "<html><body></body></html>";
  return DOCUMENT_START.test(trimmed) ? trimmed : `<html><body>${trimmed}</body></html>`;
}

/** `#id` when that is legal CSS, otherwise an equivalent attribute selector. */
export function cssId(value: string): string {
  if (CSS_IDENT.test(value)) return `#${value}`;
  return `[id="${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"]`;
}

/**
 * Escape a value for a single-quoted CSS attribute selector.
 *
 * `name="it's"` must not produce `input[name='it's']`, which is a parse error.
 */
export function cssString(value: string): string {
  return value
    .replace(/\\/g, "\\\\")
    .replace(/'/g, "\\'")
    .replace(/\n/g, "\\a ")
    .replace(/\r/g, "\\d ");
}

/**
 * A `.class` token when the name is legal CSS, else an exact-word attribute
 * selector. `class="a.b"` must not become `div.a.b` — that reads as two classes and
 * matches a *different* element — and `class="1x"` must not become `.1x`, which is
 * a parse error.
 */
export function classToken(value: string): string {
  if (CSS_IDENT.test(value)) return `.${value}`;
  return `[class~='${cssString(value)}']`;
}

export interface GraphNode {
  id: string;
  type: string;
  role: string;
  label: string;
  selector: string;
  input_type?: string;
  placeholder?: string;
  href?: string;
  required?: boolean;
  disabled?: boolean;
  content_editable?: boolean;
  card?: string;
  value?: string;
  action: "click" | "fill";
  frame?: string[];
  frame_url?: string;
  count?: number;
}

export interface GraphMetadata {
  page_title: string;
  url: string;
  total_interactive_nodes: number;
  parsing_latency_ms: number;
  warning?: string;
  frames_read?: number;
  frames_skipped?: number;
  offscreen_skipped?: number;
  occluded_skipped?: number;
  duplicates_collapsed?: number;
  /** `<input type="hidden">` payloads, collected not pruned — see parser.py. */
  hidden_fields?: Array<Record<string, string>>;
  hidden_field_count?: number;
}

/** One node as the compact DSL renders it. Shared so partial views — a search
 * result, a diff — never drift from the format the full graph prints. */
export function compactLine(node: GraphNode, selectors = false, hrefs = false): string {
  let flags = node.required ? "*" : "";
  flags += node.disabled ? "!" : "";
  let line = `[${node.id.replace(/^node_/, "")}] ${node.type}${flags} ${JSON.stringify(node.label)}`;
  if (node.count) line += ` ×${node.count}`;
  if (node.placeholder) line += ` ph=${JSON.stringify(node.placeholder)}`;
  if (hrefs && node.href) line += ` -> ${node.href}`;
  if (selectors) line += ` (${node.selector})`;
  return line;
}

/**
 * Nodes whose type or label contains every whitespace-separated term.
 *
 * Deliberately dumb: substring, case-folded, no stemming or synonyms.
 */
export function findNodes(nodes: GraphNode[], query: string): GraphNode[] {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  return nodes.filter((node) => {
    const haystack = `${node.type} ${node.label}`.toLowerCase();
    return terms.every((term) => haystack.includes(term));
  });
}

export class InteractionGraph {
  nodes: GraphNode[];
  metadata: GraphMetadata;

  constructor(nodes: GraphNode[], metadata: GraphMetadata) {
    this.nodes = nodes;
    this.metadata = metadata;
  }

  toJson(indent = 2): string {
    return JSON.stringify({ nodes: this.nodes, metadata: this.metadata }, null, indent || undefined);
  }

  /** `node_id -> css selector`, so callers can act on ids alone. */
  selectorMap(): Record<string, string> {
    const map: Record<string, string> = {};
    for (const node of this.nodes) map[node.id] = node.selector;
    return map;
  }

  /**
   * Token-dense line format for model context.
   *
   * JSON spends most of its tokens on repeated keys, quotes and braces. This drops
   * them, and by default drops the CSS selector too: a caller holding
   * `selectorMap()` can resolve `[03]` back to a selector itself, so the model
   * never needs to see it. Enable `selectors`/`hrefs` when the reader has no
   * session to resolve ids against.
   *
   * `*` marks required, `!` marks disabled.
   */
  toCompactText(options: { selectors?: boolean; hrefs?: boolean } = {}): string {
    const lines = [`PAGE: ${this.metadata.page_title} | ${this.metadata.url}`];
    if (this.metadata.offscreen_skipped) {
      lines.push(`(${this.metadata.offscreen_skipped} more nodes offscreen — scroll and re-read to see them)`);
    }
    if (this.metadata.occluded_skipped) {
      lines.push(`(${this.metadata.occluded_skipped} nodes hidden behind an overlay — dismiss it and re-read)`);
    }
    if (this.metadata.duplicates_collapsed) {
      lines.push(`(${this.metadata.duplicates_collapsed} duplicate nodes collapsed — use collapse_duplicates to expand)`);
    }
    let currentCard: string | undefined | typeof NO_CARD = NO_CARD;
    for (const node of this.nodes) {
      if (node.card !== currentCard) {
        if (node.card) lines.push(`@card ${JSON.stringify(node.card)}:`);
        currentCard = node.card;
      }
      const line = compactLine(node, options.selectors, options.hrefs);
      lines.push(node.card ? `  ${line}` : line);
    }
    return lines.join("\n");
  }
}

type SiblingCache = Map<Element, [Map<Element, number>, Map<string, number>]>;

export class ZeroDOMParser {
  private readonly url: string;
  private readonly rawHtml: string;
  private readonly viewportOnly: boolean;
  private readonly checkOcclusion: boolean;
  private readonly collapseDuplicates: boolean;
  private readonly document: Document;
  private readonly root: Element;
  private readonly nodes: GraphNode[] = [];
  offscreenSkipped = 0;
  occludedSkipped = 0;
  /** `<input type="hidden">` payloads — see the walk() collection. */
  hiddenFields: Array<Record<string, string>> = [];
  // (kind:key) -> occurrences, one pass over the tree, keyed so each kind of
  // uniqueness claim — id, tag|name, tag|class — is counted separately.
  private occ: Map<string, number> | null = null;
  // parent element -> (child -> nth-of-type position, tag -> sibling count)
  private siblings: SiblingCache = new Map();
  // parent element -> "tag|class" -> sibling count, for isStructuralCard.
  // Memoized per parent, same reasoning as `siblings` above.
  private structuralSiblings: Map<Element, Map<string, number>> = new Map();
  private paths: Map<Element, string> = new Map();
  private shadow: boolean | null = null;

  constructor(html: string, url = "about:blank", viewportOnly = false, checkOcclusion = false, collapseDuplicates = false) {
    this.url = url;
    this.rawHtml = html;
    this.checkOcclusion = checkOcclusion;
    this.viewportOnly = viewportOnly;
    this.collapseDuplicates = collapseDuplicates;
    const { document } = parseHTML(normalizeDocument(html));
    this.document = document;
    this.root = document.documentElement;
  }

  /**
   * Hidden by an inline style, an ARIA attribute, or a boolean attribute.
   *
   * `data-zerodom-hidden` is set by the browser-side serializer, the only place a
   * stylesheet rule can actually be resolved — parsing HTML text alone cannot see
   * the cascade.
   */
  private static isHidden(el: Element): boolean {
    if (el.hasAttribute("data-zerodom-hidden")) return true;
    const style = (el.getAttribute("style") ?? "").toLowerCase().replace(/\s+/g, "");
    if (style.includes("display:none") || style.includes("visibility:hidden")) return true;
    if (el.getAttribute("aria-hidden") === "true" || el.hasAttribute("hidden")) return true;
    return el.localName === "input" && el.getAttribute("type") === "hidden";
  }

  /**
   * Outside the viewport at parse time, per `data-zerodom-offscreen` — stamped
   * by playwright.ts's SERIALIZE only when `viewportOnly=true` was requested.
   * Same "only the browser knows" reasoning as `isHidden`'s `data-zerodom-hidden`.
   */
  private static isOffscreen(el: Element): boolean {
    return el.hasAttribute("data-zerodom-offscreen");
  }

  /**
   * Covered by another element at parse time, per `data-zerodom-occluded` —
   * stamped by playwright.ts's SERIALIZE only when `checkOcclusion=true` was
   * requested. Catches "element intercepts pointer events" at parse time.
   */
  private static isOccluded(el: Element): boolean {
    return el.hasAttribute("data-zerodom-occluded");
  }

  /** See `CARD_TAGS`/`CARD_ROLES` above for what counts and why. */
  private static isCard(el: Element): boolean {
    return CARD_TAGS.has(el.localName) || CARD_ROLES.has(el.getAttribute("role") ?? "");
  }

  /**
   * A component-library card with no semantic tag or role — Shadcn/Radix/
   * Tailwind dashboards build repeating cards as
   * `<div class="rounded-lg border bg-card p-6">`, not `<article>`. Three or
   * more siblings sharing the exact same tag *and* full class string is a
   * much stronger signal than tag alone (which would also fire on a plain
   * `<nav>`'s links or a toolbar) — still a heuristic, not a certainty, but
   * the existing 2-or-more-controls threshold in parse() already bounds the
   * damage of a wrong guess to an unnecessary header, never a lost or
   * misaddressed node.
   */
  private isStructuralCard(el: Element): boolean {
    // Cheapest, most-likely-to-fail check first: most elements have no
    // class at all, and skipping past them avoids parentElement lookups on
    // every single one -- this order cost the Python port its <50ms/5k-node
    // budget when reversed (measured live).
    const cls = el.getAttribute("class")?.trim();
    if (!cls) return false;
    const parent = el.parentElement;
    if (parent === null) return false;
    let cache = this.structuralSiblings.get(parent);
    if (!cache) {
      cache = new Map<string, number>();
      for (const child of Array.from(parent.children)) {
        const childCls = child.getAttribute("class")?.trim();
        if (childCls) {
          const key = `${child.localName}|${childCls}`;
          cache.set(key, (cache.get(key) ?? 0) + 1);
        }
      }
      this.structuralSiblings.set(parent, cache);
    }
    return (cache.get(`${el.localName}|${cls}`) ?? 0) >= 3;
  }

  /**
   * What to call a card in `@card "...":` — a heading, else the first link's
   * text, else the card's own text, else "" and let the caller number it.
   */
  private static cardTitle(card: Element): string {
    for (const tag of ["h1", "h2", "h3", "h4", "h5", "h6"]) {
      for (const h of Array.from(card.querySelectorAll(tag))) {
        const text = textOf(h);
        if (text) return clampCardTitle(text);
      }
    }
    const generic = new Set(["read more", "learn more", "view", "details", "more", "show more"]);
    for (const a of Array.from(card.querySelectorAll("a"))) {
      const text = textOf(a);
      if (text) {
        const t = text.trim().toLowerCase();
        if (t.length >= 3 && !generic.has(t)) return clampCardTitle(text);
      }
    }
    return clampCardTitle(textOf(card));
  }

  /**
   * A rich-text-editor root: `contenteditable` / `contenteditable="true"`.
   *
   * Notion, Slack, Discord, Jira all edit through a `<div contenteditable>`,
   * not an `<input>`/`<textarea>` — checked directly on the element, not
   * inherited from an ancestor. `contenteditable="false"` (used to carve an
   * inert island out of an editable ancestor, e.g. a mention chip) is
   * deliberately excluded.
   */
  private static isContentEditable(el: Element): boolean {
    if (!el.hasAttribute("contenteditable")) return false;
    const value = (el.getAttribute("contenteditable") ?? "").toLowerCase();
    return value === "" || value === "true";
  }

  /** Is this something an agent can act on? */
  private static isInteractive(el: Element): boolean {
    if (ZeroDOMParser.isContentEditable(el)) return true;
    if (INTERACTIVE_TAGS.has(el.localName)) {
      // A bare <a> with no href is a named anchor, not something to click.
      if (el.localName === "a" && !(el.hasAttribute("href") || el.hasAttribute("onclick"))) return false;
      // Nor is `<a href="#section" id="section"></a>` — the empty fragment anchors
      // GitHub-rendered markdown scatters through a page are link *destinations*.
      // Clicking one does nothing.
      if (
        el.localName === "a" &&
        (el.getAttribute("href") ?? "").startsWith("#") &&
        el.children.length === 0 &&
        !(el.textContent ?? "").trim()
      ) {
        return false;
      }
      return true;
    }
    const role = el.getAttribute("role");
    if (role && INTERACTIVE_ROLES.has(role)) return true;
    return el.hasAttribute("onclick") || el.hasAttribute("tabindex");
  }

  private static role(el: Element): string {
    const role = el.getAttribute("role");
    if (role) return role;
    if (el.localName === "input") return INPUT_ROLES[el.getAttribute("type") ?? "text"] ?? "textbox";
    if (ZeroDOMParser.isContentEditable(el)) return "textbox";
    return TAG_ROLES[el.localName] ?? "button";
  }

  /**
   * Occurrences per id / tag+name / tag+class, counted once per document.
   *
   * Uniqueness is only claimed against what the document actually has. A selector
   * like `input[name='color']` looks unique to the author but matches every radio
   * in the group — so `name` and `id` selectors are only emitted when their key
   * occurs exactly once.
   */
  private counts(): Map<string, number> {
    if (this.occ === null) {
      const counts = new Map<string, number>();
      const bump = (key: string) => counts.set(key, (counts.get(key) ?? 0) + 1);
      for (const el of [this.root, ...Array.from(this.root.querySelectorAll("*"))]) {
        const id = el.getAttribute("id");
        if (id) bump(`i:${id}`);
        const name = el.getAttribute("name");
        if (name) bump(`n:${el.localName}:${name}`);
        for (const cls of (el.getAttribute("class") ?? "").split(/\s+/).filter(Boolean)) {
          bump(`c:${el.localName}:${cls}`);
        }
      }
      this.occ = counts;
    }
    return this.occ;
  }

  /** Cheapest CSS selector that uniquely targets this element. */
  private selector(el: Element): string {
    const id = el.getAttribute("id");
    if (id && this.counts().get(`i:${id}`) === 1) return cssId(id);

    const name = el.getAttribute("name");
    if (name && this.counts().get(`n:${el.localName}:${name}`) === 1) {
      return `${el.localName}[name='${cssString(name)}']`;
    }

    // Only claim uniqueness when a single class already pins the element down; a
    // pair that is jointly unique is possible but not worth an xpath-equivalent per node.
    const classes = (el.getAttribute("class") ?? "").split(/\s+/).filter((c) => c && c.length < 30 && !c.includes(":"));
    for (const cls of classes) {
      if (this.counts().get(`c:${el.localName}:${cls}`) === 1) return `${el.localName}${classToken(cls)}`;
    }

    // Structural fallback: a full ancestor path is verbose but always unique, unlike
    // a bare `tag:nth-of-type(n)` which matches under every parent.
    return this.disambiguate(el, this.path(el) || el.localName);
  }

  private hasShadow(): boolean {
    if (this.shadow === null) {
      this.shadow = Array.from(this.root.querySelectorAll("template")).some(isShadowRoot);
    }
    return this.shadow;
  }

  /** The ancestor-or-self that is a direct child of a shadow root, if any. */
  private static shadowBoundary(el: Element): Element | null {
    let node = el;
    let parent = el.parentElement;
    while (parent !== null) {
      if (isShadowRoot(parent)) return node;
      node = parent;
      parent = parent.parentElement;
    }
    return null;
  }

  /**
   * Resolve light/shadow collisions that plain CSS cannot express.
   *
   * Playwright's CSS engine pierces open shadow roots, so `#host > button` matches
   * a shadow button as well as a light one — the selector looks unique in the HTML
   * and hits two elements at click time. Pages with no shadow root (nearly all of
   * them) return here untouched and pay nothing.
   */
  private disambiguate(el: Element, path: string): string {
    if (!this.hasShadow()) return path;
    const boundary = ZeroDOMParser.shadowBoundary(el);
    if (boundary === null) {
      // `:light()` is Playwright's non-piercing matcher: scope the whole path,
      // every hop, to the light tree the HTML actually described.
      return `:light(${path})`;
    }

    // A shadow child collides only at the boundary hop; above it, both trees share
    // the same ancestors. Positions are counted per tree, so an identical tag at an
    // identical position on the host is the one ambiguous case.
    const shadowParent = boundary.parentElement; // the <template shadowrootmode> itself
    const host = shadowParent?.parentElement ?? null;
    const segment = this.nthOfType(boundary, shadowParent!);
    const twin =
      host !== null &&
      Array.from(host.children).some((child) => child.localName === boundary.localName && this.nthOfType(child, host) === segment);
    // Light matches come first in Playwright's document order, so the twin is nth=1.
    return twin ? `${path} >> nth=1` : path;
  }

  /**
   * CSS path anchored at the nearest ancestor `#id`, else at the root.
   *
   * Memoized so shared ancestors are walked once, and anchoring on an id keeps the
   * selector short — a full root path on a deeply nested page costs more tokens
   * than the node it describes.
   */
  private path(el: Element): string {
    const chain: Element[] = [];
    let node: Element = el;
    let path: string;
    for (;;) {
      const cached = this.paths.get(node);
      if (cached !== undefined) {
        path = cached;
        break;
      }
      const parent = node.parentElement;
      if (parent === null) {
        // reached <html>, which we leave implicit
        path = "";
        break;
      }
      const nodeId = node.getAttribute("id");
      if (node !== el && nodeId && this.counts().get(`i:${nodeId}`) === 1) {
        path = cssId(nodeId);
        this.paths.set(node, path);
        break;
      }
      chain.push(node);
      node = parent;
    }

    for (let i = chain.length - 1; i >= 0; i--) {
      const n = chain[i];
      // The shadow root itself has no live counterpart — the browser exposes its
      // children as children of the host, and Playwright's `>` reaches them.
      if (isShadowRoot(n)) {
        this.paths.set(n, path);
        continue;
      }
      // Child combinator, not descendant: `:nth-of-type` is omitted when a tag is
      // unique among its *siblings*, which only pins the element down if every hop
      // is a direct one. Under a descendant combinator `span a` also matches an <a>
      // nested two spans deep — on Hacker News that aimed "new" at the logo.
      const parent = n.parentElement;
      let segment = parent === null ? n.localName : `${n.localName}${this.nthOfType(n, parent)}`;
      // A live DOM already has the <tbody> a <table> implies; this only matters
      // when building the path from source HTML that hasn't been through a browser
      // yet — kept for parity with a raw-source parse.
      //
      // Known limitation: a table that *mixes* authored sections with bare <tr>s
      // splits into two live <tbody> elements, and only a tbody index tells them
      // apart. That renumbering is skipped here, so rows in such tables may share
      // a selector.
      if (n.localName === "tr" && parent?.localName === "table") segment = `tbody > ${segment}`;
      path = path ? `${path} > ${segment}` : segment;
      this.paths.set(n, path);
    }
    return path;
  }

  /**
   * `:nth-of-type(n)` for a child, or "" when its tag is unique among siblings.
   *
   * Positions are indexed per parent, not rescanned per node — rescanning is O(n^2)
   * on flat pages with thousands of same-tag siblings.
   */
  private nthOfType(node: Element, parent: Element): string {
    let cache = this.siblings.get(parent);
    if (!cache) {
      const totals = new Map<string, number>();
      const positions = new Map<Element, number>();
      for (const child of Array.from(parent.children)) {
        const n = (totals.get(child.localName) ?? 0) + 1;
        totals.set(child.localName, n);
        positions.set(child, n);
      }
      cache = [positions, totals];
      this.siblings.set(parent, cache);
    }
    const [positions, totals] = cache;
    const position = positions.get(node);
    if (position === undefined || (totals.get(node.localName) ?? 0) < 2) return "";
    return `:nth-of-type(${position})`;
  }

  /**
   * Single depth-first pass: prune, collect interactive nodes and labels.
   *
   * Skipping a pruned or hidden subtree outright is both the pruning rule and the
   * fast path — nothing below it is ever visited.
   */
  private walk(): {
    found: Array<[Element, Element | null, Element | null]>;
    labelsByFor: Map<string, Element>;
  } {
    const found: Array<[Element, Element | null, Element | null]> = [];
    const labelsByFor = new Map<string, Element>();
    // [element, nearest ancestor <label>, nearest ancestor card], pushed
    // reversed so pops stay in doc order.
    const stack: Array<[Element, Element | null, Element | null]> = [[this.root, null, null]];
    this.offscreenSkipped = 0;
    this.occludedSkipped = 0;

    while (stack.length) {
      const [el, parentLabel, ancestorCard] = stack.pop()!;
      // Hidden inputs aren't interactive — but they carry payload an agent
      // may need (CSRF tokens, per-view state), so collect them before the
      // prune rather than discarding them wholesale. Mirrors hidden_fields in
      // parser.py.
      if (el.localName === "input" && el.getAttribute("type") === "hidden") {
        const hidden: Record<string, string> = {
          name: el.getAttribute("name") || "",
          value: el.getAttribute("value") || "",
          selector: this.selector(el),
        };
        const id = el.getAttribute("id");
        if (id) hidden.id = id;
        const form = el.getAttribute("form");
        if (form) hidden.form = form;
        this.hiddenFields.push(hidden);
        continue;
      }
      if ((PRUNE_TAGS.has(el.localName) && !isShadowRoot(el)) || ZeroDOMParser.isHidden(el)) continue;

      let ancestorLabel = parentLabel;
      if (el.localName === "label") {
        ancestorLabel = el;
        const forId = el.getAttribute("for");
        if (forId && !labelsByFor.has(forId)) labelsByFor.set(forId, el);
      }
      if (ZeroDOMParser.isInteractive(el)) {
        // Checked per-element, not as a subtree prune like isHidden: an
        // offscreen/occluded container can still have a position:fixed/
        // sticky child that IS onscreen and clickable, so pruning the whole
        // branch would hide it too.
        if (this.viewportOnly && ZeroDOMParser.isOffscreen(el)) {
          this.offscreenSkipped++;
        } else if (this.checkOcclusion && ZeroDOMParser.isOccluded(el)) {
          this.occludedSkipped++;
        } else {
          found.push([el, ancestorLabel, ancestorCard]);
        }
      }
      // el's own card membership (the value just used above) is the context
      // it was found IN, not itself — its children belong to el's card only
      // once el itself becomes that card, here.
      const childCard = ZeroDOMParser.isCard(el) || this.isStructuralCard(el) ? el : ancestorCard;

      const children = Array.from(el.children);
      for (let i = children.length - 1; i >= 0; i--) stack.push([children[i], ancestorLabel, childCard]);
    }
    return { found, labelsByFor };
  }

  private collapseDuplicateNodes(nodes: GraphNode[]): { nodes: GraphNode[]; dupSkipped: number } {
    if (!nodes.length) return { nodes, dupSkipped: 0 };
    // Group by *index*, not by node, so the collapse can be applied in place
    // below. Rebuilding the list by walking the groups would emit every node
    // sharing a (type, role, label) together and silently reorder the whole
    // graph -- which fragments the @card runs in toCompactText() (the same card
    // header repeats once per run, costing more tokens than the collapse saves)
    // and hands the model a node order that no longer matches the page.
    // Mirrors _collapse_duplicate_nodes in parser.py.
    const groups = new Map<string, number[]>();
    nodes.forEach((n, i) => {
      const key = `${n.type}|${n.role}|${n.label}`;
      const arr = groups.get(key) ?? [];
      arr.push(i);
      groups.set(key, arr);
    });

    const drop = new Set<number>();
    const counts = new Map<number, number>();
    for (const idxs of groups.values()) {
      if (idxs.length < 3) continue;
      // Collapse only when nothing differentiates the members: a shared label
      // is not enough if they sit in different cards or point somewhere different.
      const first = nodes[idxs[0]];
      const hasDiff = idxs.slice(1).some(
        j => nodes[j].card !== first.card || nodes[j].href !== first.href || nodes[j].placeholder !== first.placeholder
      );
      if (hasDiff) continue;
      counts.set(idxs[0], idxs.length);  // keep the first id, for selector stability
      for (const j of idxs.slice(1)) drop.add(j);
    }

    const collapsed = nodes
      .map((n, i) => (counts.has(i) ? { ...n, count: counts.get(i) } : n))
      .filter((_, i) => !drop.has(i));
    return { nodes: collapsed, dupSkipped: drop.size };
  }

  parse(): InteractionGraph {
    const start = performance.now();

    const titleEl = this.document.querySelector("title");
    const title = titleEl ? textOf(titleEl) : "";

    const { found, labelsByFor } = this.walk();
    const linker = new LabelLinker(this.document, labelsByFor);
    const cardTitles = new Map<Element, string>();
    // A card with only one interactive descendant has nothing to
    // disambiguate — that one node's own label already says what it is.
    const cardCounts = new Map<Element, number>();
    for (const [, , card] of found) {
      if (card) cardCounts.set(card, (cardCounts.get(card) ?? 0) + 1);
    }

    for (const [el, ancestorLabel, ancestorCard] of found) {
      const role = ZeroDOMParser.role(el);
      const node: GraphNode = {
        id: `node_${String(this.nodes.length + 1).padStart(2, "0")}`,
        type: el.localName,
        role,
        label: linker.label(el, ancestorLabel),
        selector: this.selector(el),
        action: FILLABLE_ROLES.has(role) ? "fill" : "click",
      };
      if (ancestorCard !== null && (cardCounts.get(ancestorCard) ?? 0) >= 2) {
        if (!cardTitles.has(ancestorCard)) {
          cardTitles.set(ancestorCard, ZeroDOMParser.cardTitle(ancestorCard) || `card ${cardTitles.size + 1}`);
        }
        node.card = cardTitles.get(ancestorCard);
      }
      if (el.localName === "input") node.input_type = el.getAttribute("type") ?? "text";
      const placeholder = el.getAttribute("placeholder");
      if (placeholder) node.placeholder = placeholder;
      const href = el.getAttribute("href");
      if (href) node.href = href;
      if (el.hasAttribute("required")) node.required = true;
      if (el.hasAttribute("disabled")) node.disabled = true;
      if (ZeroDOMParser.isContentEditable(el)) node.content_editable = true;
      if (FILLABLE_ROLES.has(role)) {
        node.value = el.localName === "input" ? el.getAttribute("value") ?? "" : directText(el);
      }
      this.nodes.push(node);
    }

    if (this.collapseDuplicates) {
      const { nodes: collapsed, dupSkipped } = this.collapseDuplicateNodes(this.nodes);
      this.nodes.length = 0;
      this.nodes.push(...collapsed);
      if (dupSkipped) {
        // metadata will be set later
        (this as any)._dupSkipped = dupSkipped;
      }
    }

    const metadata: GraphMetadata = {
      page_title: title || "Untitled",
      url: this.url,
      total_interactive_nodes: this.nodes.length,
      parsing_latency_ms: Math.round((performance.now() - start) * 100) / 100,
    };
    if (this.offscreenSkipped) metadata.offscreen_skipped = this.offscreenSkipped;
    if (this.occludedSkipped) metadata.occluded_skipped = this.occludedSkipped;
    if (this.hiddenFields.length) {
      metadata.hidden_fields = this.hiddenFields;
      metadata.hidden_field_count = this.hiddenFields.length;
    }
    const dupSkipped = (this as any)._dupSkipped as number | undefined;
    if (dupSkipped) metadata.duplicates_collapsed = dupSkipped;
    const warning = this.emptyPageWarning(this.rawHtml.length);
    if (warning) metadata.warning = warning;
    return new InteractionGraph(this.nodes, metadata);
  }

  /**
   * Say *why* the graph looks empty, when it does.
   *
   * A bot wall, an open modal and a genuinely bare page all return almost nothing,
   * and the caller cannot tell them apart from the node list.
   */
  private emptyPageWarning(htmlBytes: number): string | null {
    if (this.nodes.length > 2) return null;
    if (htmlBytes < 4000) {
      return "Almost no interactive nodes, and the page is tiny — this is usually a bot wall or an error page, not the site you wanted.";
    }
    const inert = this.root.querySelectorAll('[aria-hidden="true"] a, [inert] a');
    if (inert.length) {
      return `Almost no interactive nodes, but ${inert.length} links sit under aria-hidden/inert — a modal or overlay is open. Dismiss it and re-read.`;
    }
    return "Almost no interactive nodes on a large page — the content is probably in an iframe (not traversed), a closed shadow root, or rendered to canvas.";
  }
}

/** Leading text directly inside an element, before its first child tag — not the full subtree text. */
function directText(el: Element): string {
  const first = el.firstChild;
  return first && first.nodeType === 3 ? (first as unknown as CharacterData).data : "";
}

/** Convenience one-shot parse. */
export function parseHtml(
  html: string, url = "about:blank", viewportOnly = false, checkOcclusion = false, collapseDuplicates = false
): InteractionGraph {
  return new ZeroDOMParser(html, url, viewportOnly, checkOcclusion, collapseDuplicates).parse();
}
