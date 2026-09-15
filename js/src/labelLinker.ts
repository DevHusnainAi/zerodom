/** Work out what a control is called, the way a person reading the page would. Port of zerodom/label_linker.py. */

// Attribute fallbacks, most explicit first. Split around the adjacent-text scan:
// authored attributes beat scraped text, but scraped text beats name/value, which
// are developer identifiers ("q") rather than anything a user reads.
const AUTHORED_ATTRS = ["aria-label", "placeholder", "alt", "title"] as const;
const IDENTIFIER_ATTRS = ["value", "name"] as const;

// Icon buttons routinely put their only human-readable caption in a tooltip
// library's data attribute. These four cover Tippy, Bootstrap and the
// conventional hand-rolled spelling, which between them are most of the web.
const TOOLTIP_ATTRS = ["data-tooltip", "data-original-title", "data-tippy-content", "data-tippy-simple-content"];

// Controls that sit next to their caption. Buttons and links carry their own text.
const CAPTIONED_TAGS = new Set(["input", "select", "textarea"]);

// Inputs whose `value` is a button caption rather than user data.
const BUTTON_INPUT_TYPES = new Set(["submit", "button", "reset", "image"]);

const MAX_LABEL_LEN = 80;

// Trailing punctuation on a caption: "Search:" -> "Search".
const CAPTION_STRIP = /[ \t\n :*>\-–—]+$/;

export function textOf(el: Element): string {
  return collapse(el.textContent ?? "").slice(0, MAX_LABEL_LEN);
}

function collapse(text: string): string {
  return text.split(/\s+/).filter(Boolean).join(" ");
}

/** Raw text between `afterEl` (exclusive) and `stopEl` — the DOM analogue of lxml's `.tail`. */
function textBetween(afterEl: Element | null, parent: Element, stopEl: Element): string {
  let node: ChildNode | null = afterEl ? afterEl.nextSibling : parent.firstChild;
  let text = "";
  while (node && node !== stopEl) {
    if (node.nodeType === 3 /* TEXT_NODE */) text += (node as unknown as CharacterData).data;
    node = node.nextSibling;
  }
  return text;
}

/**
 * Caption sitting immediately before an input, as in `Search: <input>`.
 *
 * Reads the text between the previous element sibling and this element (or the
 * parent's leading text when this is the first child). Only the last few words are
 * kept — the text run before an input is often a whole sentence of surrounding
 * copy, not a caption.
 */
export function adjacentText(el: Element): string {
  const parent = el.parentElement;
  if (!parent) return "";
  const previous = el.previousElementSibling;
  const source = textBetween(previous, parent, el);
  const caption = collapse(source).replace(CAPTION_STRIP, "");
  if (!caption) return "";
  return caption.split(" ").slice(-6).join(" ").slice(0, MAX_LABEL_LEN);
}

// Handler names that describe the plumbing rather than the action.
const HANDLER_NOISE = new Set([
  "return", "function", "void", "this", "event", "typeof", "new", "if",
  "settimeout", "requestanimationframe", "preventdefault", "stoppropagation",
]);
const HANDLER_CALL = /([A-Za-z_$][\w$]*)\s*\(/g;
const WORD_BREAK = /(?<=[a-z0-9])(?=[A-Z])/g;

/**
 * The action an inline handler names, as words. `_bsa.close(…)` -> "close".
 *
 * Only ever reached when an element has no text, no icon and no attribute of its
 * own, which in practice means an icon `<div onclick>` built out of an SVG path.
 * Conservative on purpose: a name that describes the wiring instead of the intent
 * is worse than admitting the control is unlabelled.
 */
function handlerLabel(onclick: string): string {
  const names = Array.from((onclick || "").matchAll(HANDLER_CALL), (m) => m[1]).reverse();
  for (const name of names) {
    if (name.length < 3 || HANDLER_NOISE.has(name.toLowerCase().replace(/^_+/, ""))) continue;
    const words = name.replace(/^[_$]+|[_$]+$/g, "").replace(WORD_BREAK, " ").replace(/[_-]/g, " ");
    const cleaned = collapse(words).toLowerCase();
    if (cleaned) return cleaned.slice(0, MAX_LABEL_LEN);
  }
  return "";
}

/**
 * A link's destination, trimmed to the part a person would read aloud.
 *
 * Last resort only — a real caption always wins. `javascript:` and bare fragments
 * say nothing about where the link goes, so they stay unlabelled.
 */
function hrefLabel(href: string): string {
  const trimmed0 = href.trim();
  if (trimmed0.startsWith("javascript:") || trimmed0.startsWith("#") || !trimmed0) return "";
  // Drop scheme and www so "https://news.ycombinator.com/" reads as the site.
  const trimmed = trimmed0.replace(/^[a-z][a-z0-9+.-]*:\/\/(www\.)?/i, "");
  return trimmed.replace(/\/+$/, "").slice(0, MAX_LABEL_LEN);
}

/**
 * Resolves labels against a pre-indexed set of <label> tags.
 *
 * The parser walks the DOM once and hands over what it saw, so label lookup stays
 * O(1) per node instead of re-searching the tree for every input.
 */
export class LabelLinker {
  private readonly root: Document;
  private readonly labelsByFor: Map<string, Element>;

  constructor(root: Document, labelsByFor?: Map<string, Element>) {
    this.root = root;
    if (labelsByFor) {
      this.labelsByFor = labelsByFor;
    } else {
      this.labelsByFor = new Map();
      for (const tag of Array.from(root.querySelectorAll("label"))) {
        const forId = tag.getAttribute("for");
        if (forId && !this.labelsByFor.has(forId)) this.labelsByFor.set(forId, tag);
      }
    }
  }

  label(element: Element, ancestorLabel: Element | null = null): string {
    // explicit <label for="...">
    const elemId = element.getAttribute("id");
    if (elemId) {
      const tag = this.labelsByFor.get(elemId);
      if (tag) {
        const text = textOf(tag);
        if (text) return text;
      }
    }

    // wrapping <label>
    let ancestor = ancestorLabel;
    if (!ancestor) {
      let node = element.parentElement;
      while (node) {
        if (node.localName === "label") {
          ancestor = node;
          break;
        }
        node = node.parentElement;
      }
    }
    if (ancestor) {
      const text = textOf(ancestor);
      if (text) return text;
    }

    // aria-labelledby -> referenced element's text
    for (const refId of (element.getAttribute("aria-labelledby") ?? "").split(/\s+/).filter(Boolean)) {
      const ref = this.root.getElementById(refId);
      if (ref) {
        const text = textOf(ref);
        if (text) return text;
      }
    }

    // authored attribute fallbacks
    for (const attr of AUTHORED_ATTRS) {
      const val = element.getAttribute(attr);
      if (val && val.trim()) return val.trim().slice(0, MAX_LABEL_LEN);
    }

    // On a submit/button input, `value` is the visible caption, not an identifier.
    if (element.localName === "input" && BUTTON_INPUT_TYPES.has(element.getAttribute("type") ?? "")) {
      const val = element.getAttribute("value");
      if (val && val.trim()) return val.trim().slice(0, MAX_LABEL_LEN);
    }

    // Caption text next to the control, before falling back to identifiers
    if (CAPTIONED_TAGS.has(element.localName)) {
      const caption = adjacentText(element);
      if (caption) return caption;
    }

    // Inner text (buttons, links). Skipped for captioned controls, whose contents
    // are their value or options — a <select>'s first <option> is not its label.
    if (!CAPTIONED_TAGS.has(element.localName)) {
      const text = textOf(element);
      if (text) return text;
    }

    for (const attr of IDENTIFIER_ATTRS) {
      const val = element.getAttribute(attr);
      if (val && val.trim()) return val.trim().slice(0, MAX_LABEL_LEN);
    }

    // Icon-only controls carry their label on a descendant, not on themselves: an
    // <img alt>, but just as often a styled <div title> or <i aria-label>. Hacker
    // News' vote arrows are <a><div class="votearrow" title="upvote"></div></a>,
    // and they are the most-clicked control on the page.
    for (const descendant of Array.from(element.querySelectorAll("*"))) {
      for (const attr of AUTHORED_ATTRS) {
        const val = descendant.getAttribute(attr);
        if (val && val.trim()) return val.trim().slice(0, MAX_LABEL_LEN);
      }
      // <svg><title>Save</title></svg> is the standards-defined accessible name
      // for an inline icon. The walk prunes <svg>, but the label lookup reads it
      // directly.
      if ((descendant.localName === "title" || descendant.localName === "desc")) {
        const text = textOf(descendant);
        if (text) return text;
      }
    }

    // Tooltip libraries, checked after real markup and before giving up.
    for (const attr of TOOLTIP_ATTRS) {
      const val = element.getAttribute(attr);
      if (val && val.trim()) return val.trim().slice(0, MAX_LABEL_LEN);
    }

    // A link with no text and no icon label still has somewhere to go, and the
    // destination is the only thing left that tells an agent them apart.
    if (element.localName === "a") {
      const href = (element.getAttribute("href") ?? "").trim();
      if (href) {
        const label = hrefLabel(href);
        if (label) return label;
      }
    }

    // Last resort: the handler's own name. A bare `<div onclick="_bsa.close(…)">`
    // wrapping an SVG path has no text, no alt, no title and no tooltip — but
    // somebody named the function, and "close" is worth more to an agent than
    // "Unlabelled Element".
    const handler = handlerLabel(element.getAttribute("onclick") ?? "");
    if (handler) return handler;

    return "Unlabelled Element";
  }
}

/** Standalone label lookup for a single element. */
export function getLabel(root: Document, element: Element): string {
  return new LabelLinker(root).label(element);
}
