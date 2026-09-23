"""Core DOM-to-Interaction-Graph engine.

Built on lxml.html rather than BeautifulSoup: bs4's tree construction alone costs
~60ms on a 5k-node page, which blows the latency budget before any work is done.
lxml parses the same page in ~5ms with an equivalent element API.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from typing import Any

import lxml.html
from lxml.html import HtmlElement

from .label_linker import LabelLinker, text_of

# `head` is deliberately absent: everything bloated inside it is pruned by name
# anyway, and lxml parks stray content (a <button> after <title>) in head where a
# browser would have moved it into body. Skipping head wholesale loses those nodes.
PRUNE_TAGS = {"script", "style", "link", "svg", "meta", "noscript", "template"}


def is_shadow_root(el: HtmlElement) -> bool:
    """A declarative shadow root: `<template shadowrootmode="open">`.

    Inert `<template>` content is pruned, but this one is a live subtree the browser
    renders — Chromium serializes open shadow roots back into exactly this form.
    """
    return el.tag == "template" and "shadowrootmode" in el.attrib


INTERACTIVE_TAGS = {"a", "button", "input", "select", "textarea"}
INTERACTIVE_ROLES = {
    "button", "link", "textbox", "checkbox", "radio", "combobox",
    "searchbox", "switch", "menuitem", "tab", "option", "slider",
}

# input[type] -> ARIA role. Anything unlisted (text, email, password, ...) is a textbox.
INPUT_ROLES = {
    "checkbox": "checkbox",
    "radio": "radio",
    "submit": "button",
    "button": "button",
    "reset": "button",
    "image": "button",
    "file": "button",  # ARIA: opens a chooser — clickable, not fillable
    "search": "searchbox",
    "range": "slider",
}
TAG_ROLES = {"a": "link", "button": "button", "select": "combobox", "textarea": "textbox"}

# Roles driven by typing text rather than clicking.
FILLABLE_ROLES = {"textbox", "searchbox", "slider"}

# A "card" is the nearest ancestor that groups one repeated list/feed item —
# used to disambiguate identically-labeled controls (twenty "Upvote" buttons on
# a feed) by grouping each under its own item's title in to_compact_text().
# Deliberately semantic-only, not a guessed div/class pattern: <article>/<li>
# are the direct HTML spec meaning of "one item in a list," <tr> is a table
# row (Hacker News's story rows), and the ARIA roles are their explicit
# non-tag equivalents. NOT <form>: a form's fields are usually each uniquely
# labeled already (Email/Password/Remember me), so it isn't a repeated-item
# container in the sense this exists for — grouping one every-page login form
# under a header added noise with nothing to disambiguate, caught by
# test_compact_text_format expecting the plain, ungrouped output it always
# had. A site whose list items are bare, roleless <div>s won't get grouped
# either — no confident way to tell "item wrapper" from "layout wrapper" from
# tag/class alone, and a wrong guess is worse than no grouping.
CARD_TAGS = {"article", "li", "tr"}
CARD_ROLES = {"article", "listitem", "row"}

# A card title is pure repeated overhead (one extra line per card, not per
# node), so it gets a tighter cap than a regular node label's MAX_LABEL_LEN
# (80) — a heading that's actually a whole unformatted paragraph would
# otherwise inflate every card by that much.
CARD_TITLE_MAX_LEN = 40


# A CSS identifier cannot start with a digit, so `#49151933` is a parse error, not a
# miss — querySelector throws on it. Hacker News numbers every row that way.
_CSS_IDENT = re.compile(r"^-?[_a-zA-Z][_a-zA-Z0-9-]*$")


def css_id(value: str) -> str:
    """`#id` when that is legal CSS, otherwise an equivalent attribute selector."""
    if _CSS_IDENT.match(value):
        return f"#{value}"
    return '[id="{}"]'.format(value.replace("\\", "\\\\").replace('"', '\\"'))


def css_string(value: str) -> str:
    """Escape a value for a single-quoted CSS attribute selector.

    `name="it's"` must not produce `input[name='it's']`, which is a parse error.
    """
    return (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\a ")
        .replace("\r", "\\d ")
    )


def class_token(value: str) -> str:
    """A `.class` token when the name is legal CSS, else an exact-word attribute
    selector. `class="a.b"` must not become `div.a.b` — that reads as two classes
    and matches a *different* element — and `class="1x"` must not become `.1x`,
    which is a parse error.
    """
    if _CSS_IDENT.match(value):
        return f".{value}"
    return f"[class~='{css_string(value)}']"


_NO_CARD = object()  # sentinel so the first node (whose card may be None) always prints its header


def _clamp_card_title(text: str) -> str:
    if not text or len(text) <= CARD_TITLE_MAX_LEN:
        return text
    return text[: CARD_TITLE_MAX_LEN - 1].rstrip() + "…"


class InteractionGraph(dict):
    """Parsed graph. A plain dict, so callers can treat it as one, with serializers."""

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self, indent=indent, ensure_ascii=False)

    def to_html_report(self, png: bytes | None = None, layout: dict[str, Any] | None = None) -> str:
        """Self-contained HTML inspector: graph on the left, annotated page on the right.

        `png` and `layout` come from `zerodom.report.screenshot(page, graph)`; without
        them the report still renders, just with no picture to point at.
        """
        from .report import html_report  # local: report.py is browser-facing, parser is not

        return html_report(self, png, layout)

    def selector_map(self) -> dict[str, str]:
        """`node_id -> css selector`, so callers can act on ids alone."""
        return {node["id"]: node["selector"] for node in self["nodes"]}

    def to_compact_text(self, selectors: bool = False, hrefs: bool = False) -> str:
        """Token-dense line format for model context.

        JSON spends most of its tokens on repeated keys, quotes and braces. This
        drops them, and by default drops the CSS selector too: a caller holding
        `selector_map()` can resolve `[03]` back to a selector itself, so the model
        never needs to see it. Enable `selectors`/`hrefs` when the reader has no
        session to resolve ids against.

        `*` marks required, `!` marks disabled. A run of nodes sharing a
        `card` (see `CARD_TAGS`/`CARD_ROLES` in parser.py) is grouped under
        one `@card "title":` header instead of repeating ambiguous labels —
        twenty identical `button 'Upvote'` lines with no way to tell which
        story each belongs to.
        """
        meta = self["metadata"]
        lines = [f"PAGE: {meta['page_title']} | {meta['url']}"]
        if skipped := meta.get("offscreen_skipped"):
            lines.append(f"({skipped} more nodes offscreen — scroll and re-read to see them)")
        if skipped := meta.get("occluded_skipped"):
            lines.append(f"({skipped} nodes hidden behind an overlay — dismiss it and re-read)")
        if dup := meta.get("duplicates_collapsed"):
            lines.append(f"({dup} duplicate nodes collapsed — use collapse_duplicates=False to expand)")
        current_card = _NO_CARD
        for node in self["nodes"]:
            card = node.get("card")
            if card != current_card:
                if card:
                    lines.append(f"@card {card!r}:")
                current_card = card
            line = compact_line(node, selectors, hrefs)
            lines.append(f"  {line}" if card else line)
        return "\n".join(lines)


def find_nodes(nodes: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Nodes whose type or label contains every whitespace-separated term.

    Deliberately dumb: substring, case-folded, no stemming or synonyms. A model
    searching for "checkout" wants the node it just read a label for, not a ranked
    guess. Shared by the CLI's --find and the MCP tool so they can't drift.
    """
    terms = query.lower().split()
    return [
        node
        for node in nodes
        if all(term in f"{node['type']} {node['label']}".lower() for term in terms)
    ]


def compact_line(node: dict[str, Any], selectors: bool = False, hrefs: bool = False) -> str:
    """One node as the compact DSL renders it. Shared so partial views — a search
    result, a diff — never drift from the format the full graph prints."""
    flags = "*" if node.get("required") else ""
    flags += "!" if node.get("disabled") else ""
    line = f"[{node['id'].removeprefix('node_')}] {node['type']}{flags} {node['label']!r}"
    if count := node.get("count"):
        line += f" ×{count}"
    if placeholder := node.get("placeholder"):
        line += f" ph={placeholder!r}"
    if hrefs and (href := node.get("href")):
        line += f" -> {href}"
    if selectors:
        line += f" ({node['selector']})"
    return line


class ZeroDOMParser:
    """Parses raw HTML into a token-optimized Interaction Graph."""

    def __init__(
        self,
        html: str,
        url: str = "about:blank",
        viewport_only: bool = False,
        check_occlusion: bool = False,
        collapse_duplicates: bool = False,
    ) -> None:
        self.raw_html = html
        self.url = url
        self.viewport_only = viewport_only
        self.check_occlusion = check_occlusion
        self.collapse_duplicates = collapse_duplicates
        self.offscreen_skipped = 0
        self.occluded_skipped = 0
        self.root: HtmlElement = lxml.html.document_fromstring(html.strip() or "<html></html>")
        self.nodes: list[dict[str, Any]] = []
        # (kind, ...) -> occurrences, one pass over the tree, keyed so each kind of
        # uniqueness claim — id, (tag, name), (tag, class) — is counted separately.
        self._occ: Counter[tuple[str, ...]] | None = None
        # parent element -> (child -> nth-of-type position, tag -> sibling count)
        self._siblings: dict[HtmlElement, tuple[dict[HtmlElement, int], Counter[str]]] = {}
        self._paths: dict[HtmlElement, str] = {}
        self._shadow: bool | None = None
        # parent element -> (tag, class string) -> sibling count, for
        # _is_structural_card. Memoized per parent, same reasoning as
        # _siblings above: rescanning per child is O(n^2) on a flat page.
        self._structural_siblings: dict[HtmlElement, Counter[tuple[str, str]]] = {}
        # `<input type="hidden">` fields, collected rather than pruned: they
        # carry CSRF tokens and per-view state the agent may need to submit a
        # form, but stay OUT of the interaction graph (they're not clickable,
        # so listing them as nodes would be pure noise). See `hidden_fields` in
        # parse().
        self.hidden_fields: list[dict[str, Any]] = []

    @staticmethod
    def _is_hidden(el: HtmlElement) -> bool:
        """Hidden by an inline style, an ARIA attribute, or a boolean attribute.

        `data-zerodom-hidden` is set by the browser-side serializer, which is the
        only place a stylesheet rule can actually be resolved — see SERIALIZE in
        playwright_wrapper. Parsing HTML text alone cannot see the cascade.
        """
        if "data-zerodom-hidden" in el.attrib:
            return True
        style = el.get("style", "").lower().replace(" ", "")
        if "display:none" in style or "visibility:hidden" in style:
            return True
        if el.get("aria-hidden") == "true" or "hidden" in el.attrib:
            return True
        return el.tag == "input" and el.get("type") == "hidden"

    @staticmethod
    def _is_offscreen(el: HtmlElement) -> bool:
        """Outside the viewport at parse time, per `data-zerodom-offscreen` —

        stamped by playwright_wrapper.SERIALIZE only when `viewport_only=True`
        was requested (getBoundingClientRect per element isn't free, so it's
        never computed when nothing will use it). Same "only the browser
        knows" reasoning as `_is_hidden`'s `data-zerodom-hidden`: a parser
        reading HTML text alone has no viewport to check against.
        """
        return "data-zerodom-offscreen" in el.attrib

    @staticmethod
    def _is_occluded(el: HtmlElement) -> bool:
        """Covered by another element (a modal backdrop, an open dropdown, a
        cookie banner) at parse time, per `data-zerodom-occluded` — stamped by
        playwright_wrapper.SERIALIZE only when `check_occlusion=True` was
        requested (an elementFromPoint() hit-test per element isn't free
        either). Clicking a listed-but-covered node is exactly what makes
        Playwright throw "element intercepts pointer events" — this catches
        it at parse time instead of at click time.
        """
        return "data-zerodom-occluded" in el.attrib

    @staticmethod
    def _is_content_editable(el: HtmlElement) -> bool:
        """A rich-text-editor root: `contenteditable` / `contenteditable="true"`.

        Notion, Slack, Discord, Jira all edit through a `<div contenteditable>`,
        not an `<input>`/`<textarea>` — checked directly on the element, not
        inherited from an ancestor. `contenteditable="false"` (used to carve an
        inert island out of an editable ancestor, e.g. a mention chip) is
        deliberately excluded — unlike `data-zerodom-hidden`'s cascade, this one
        the HTML text alone can answer correctly, so there's no need to punt it
        to the browser-side serializer.
        """
        value = el.get("contenteditable")
        return value is not None and value.lower() in ("", "true")

    @staticmethod
    def _is_card(el: HtmlElement) -> bool:
        """See `CARD_TAGS`/`CARD_ROLES` above for what counts and why."""
        return el.tag in CARD_TAGS or el.get("role") in CARD_ROLES

    def _is_structural_card(self, el: HtmlElement) -> bool:
        """A component-library card with no semantic tag or role — Shadcn/
        Radix/Tailwind dashboards (Salesforce, Jira-style admin UIs) build
        repeating cards as `<div class="rounded-lg border bg-card p-6">`,
        not `<article>`. Three or more siblings sharing the exact same tag
        *and* full class string is a much stronger signal than tag alone
        (which would also fire on a plain `<nav>`'s links or a toolbar) —
        still a heuristic, not a certainty, but the existing 2-or-more-
        controls threshold in parse() already bounds the damage of a wrong
        guess to an unnecessary header, never a lost or misaddressed node.
        """
        # Cheapest, most-likely-to-fail check first: most elements in a real
        # page have no class at all, and skipping straight past them avoids
        # paying for getparent() on every single one -- measured live, the
        # other order alone cost the 5000-node benchmark its <50ms budget.
        cls = el.get("class", "").strip()
        if not cls:
            return False
        parent = el.getparent()
        if parent is None:
            return False
        cache = self._structural_siblings.get(parent)
        if cache is None:
            counts: Counter[tuple[str, str]] = Counter()
            for child in parent:
                if isinstance(child.tag, str) and (child_cls := child.get("class", "").strip()):
                    counts[(child.tag, child_cls)] += 1
            cache = self._structural_siblings[parent] = counts
        return cache[(el.tag, cls)] >= 3

    @staticmethod
    def _card_title(card: HtmlElement) -> str:
        """What to call a card in `@card "...":` — a heading, else the first
        link's text (a feed item's own title is very often its own link, e.g.
        Hacker News's `.titlelink`), else the card's own text, else "" and let
        the caller number it. Doesn't stop at a nested card's boundary —
        rare in practice, and a nested card's heading briefly borrowed as its
        parent's is a much smaller miss than no title at all.
        """
        for h in card.iter("h1", "h2", "h3", "h4", "h5", "h6"):
            if text := text_of(h):
                return _clamp_card_title(text)
        # Skip generic link texts that would make every card look the same
        generic = {"read more", "learn more", "view", "details", "more", "show more"}
        for a in card.iter("a"):
            if text := text_of(a):
                t = text.strip().lower()
                if len(t) >= 3 and t not in generic:
                    return _clamp_card_title(text)
        return _clamp_card_title(text_of(card))

    @staticmethod
    def _is_interactive(el: HtmlElement) -> bool:
        """Is this something an agent can act on?"""
        if ZeroDOMParser._is_content_editable(el):
            return True
        if el.tag in INTERACTIVE_TAGS:
            # A bare <a> with no href is a named anchor, not something to click.
            if el.tag == "a" and not ("href" in el.attrib or "onclick" in el.attrib):
                return False
            # Nor is `<a href="#section" id="section"></a>` — the empty fragment
            # anchors GitHub-rendered markdown scatters through a page are link
            # *destinations*. Clicking one does nothing, and PyPI's project page
            # alone contributes nine of them.
            if (
                el.tag == "a"
                and el.get("href", "").startswith("#")
                and len(el) == 0
                and not (el.text or "").strip()
            ):
                return False
            return True
        if el.get("role") in INTERACTIVE_ROLES:
            return True
        if "onclick" in el.attrib:
            return True
        # tabindex="-1" means "focusable via .focus() only, not part of the
        # keyboard tab order" — a standard pattern for focus-management
        # wrappers (modals, route-change targets), never a real control.
        # Confirmed on google.com/maps: both <body tabindex="-1"> and a
        # tabindex="-1" div wrapping 7 real, separately-interactive buttons
        # were misclassified as their own clickable nodes this way — the
        # body's "label" fell through to its raw <script> text (see
        # label_linker.text_of), and the wrapper duplicated its own
        # children's content into one incoherent node. Only tabindex >= 0
        # is a real interactivity signal.
        tabindex = el.get("tabindex")
        if tabindex is not None:
            try:
                return int(tabindex) >= 0
            except ValueError:
                return False
        return False

    @staticmethod
    def _role(el: HtmlElement) -> str:
        if role := el.get("role"):
            return role
        if el.tag == "input":
            return INPUT_ROLES.get(el.get("type", "text"), "textbox")
        if ZeroDOMParser._is_content_editable(el):
            return "textbox"
        return TAG_ROLES.get(el.tag, "button")

    def _counts(self) -> Counter[tuple[str, ...]]:
        """Occurrences per id / (tag, name) / (tag, class), counted once per document.

        Uniqueness is only claimed against what the document actually has. A
        selector like `input[name='color']` looks unique to the author but matches
        every radio in the group — so `name` and `id` selectors are only emitted
        when their key occurs exactly once.
        """
        if self._occ is None:
            counts: Counter[tuple[str, ...]] = Counter()
            for el in self.root.iter():
                if not isinstance(el.tag, str):
                    continue
                if el_id := el.get("id"):
                    counts[("i", el_id)] += 1
                if name := el.get("name"):
                    counts[("n", el.tag, name)] += 1
                for cls in el.get("class", "").split():
                    counts[("c", el.tag, cls)] += 1
            self._occ = counts
        return self._occ

    def _selector(self, el: HtmlElement) -> str:
        """Cheapest CSS selector that uniquely targets this element."""
        if (el_id := el.get("id")) and self._counts()[("i", el_id)] == 1:
            return css_id(el_id)
        if (name := el.get("name")) and self._counts()[("n", el.tag, name)] == 1:
            return f"{el.tag}[name='{css_string(name)}']"

        # Only claim uniqueness when a single class already pins the element down;
        # a pair that is jointly unique is possible but not worth an xpath per node.
        classes = [c for c in el.get("class", "").split() if len(c) < 30 and ":" not in c]
        for c in classes:
            if self._counts()[("c", el.tag, c)] == 1:
                return f"{el.tag}{class_token(c)}"

        # Structural fallback: a full ancestor path is verbose but always unique,
        # unlike a bare `tag:nth-of-type(n)` which matches under every parent.
        return self._disambiguate(el, self._path(el) or el.tag)

    @property
    def _has_shadow(self) -> bool:
        if self._shadow is None:
            self._shadow = any(is_shadow_root(el) for el in self.root.iter("template"))
        return self._shadow

    @staticmethod
    def _shadow_boundary(el: HtmlElement) -> HtmlElement | None:
        """The ancestor-or-self that is a direct child of a shadow root, if any."""
        node, parent = el, el.getparent()
        while parent is not None:
            if is_shadow_root(parent):
                return node
            node, parent = parent, parent.getparent()
        return None

    def _disambiguate(self, el: HtmlElement, path: str) -> str:
        """Resolve light/shadow collisions that plain CSS cannot express.

        Playwright's CSS engine pierces open shadow roots, so `#host > button` matches
        a shadow button as well as a light one — the selector looks unique in the HTML
        and hits two elements at click time. Pages with no shadow root (nearly all of
        them) return here untouched and pay nothing.
        """
        if not self._has_shadow:
            return path
        boundary = self._shadow_boundary(el)
        if boundary is None:
            # `:light()` is Playwright's non-piercing matcher: scope the whole path,
            # every hop, to the light tree the HTML actually described.
            return f":light({path})"

        # A shadow child collides only at the boundary hop; above it, both trees share
        # the same ancestors. Positions are counted per tree, so an identical tag at an
        # identical position on the host is the one ambiguous case.
        host = boundary.getparent().getparent()
        segment = self._nth_of_type(boundary, boundary.getparent())
        twin = host is not None and any(
            child.tag == boundary.tag and self._nth_of_type(child, host) == segment
            for child in host
            if isinstance(child.tag, str)
        )
        # Light matches come first in Playwright's document order, so the twin is nth=1.
        return f"{path} >> nth=1" if twin else path

    def _path(self, el: HtmlElement) -> str:
        """CSS path anchored at the nearest ancestor `#id`, else at the root.

        Memoized so shared ancestors are walked once, and anchoring on an id keeps
        the selector short — a full root path on a deeply nested page costs more
        tokens than the node it describes.
        """
        chain: list[HtmlElement] = []
        node: HtmlElement = el
        while True:
            if node in self._paths:
                path = self._paths[node]
                break
            if node.getparent() is None:  # reached <html>, which we leave implicit
                path = ""
                break
            if (
                node is not el
                and (node_id := node.get("id"))
                and self._counts()[("i", node_id)] == 1
            ):
                path = self._paths[node] = css_id(node_id)
                break
            chain.append(node)
            node = node.getparent()

        for node in reversed(chain):
            # Child combinator, not descendant: `:nth-of-type` is omitted when a tag is
            # unique among its *siblings*, which only pins the element down if every hop
            # is a direct one. Under a descendant combinator `span a` also matches an <a>
            # nested two spans deep — on Hacker News that aimed "new" at the logo.
            # The shadow root itself has no live counterpart — the browser exposes its
            # children as children of the host, and Playwright's `>` reaches them.
            if is_shadow_root(node):
                self._paths[node] = path
                continue
            parent = node.getparent()
            segment = node.tag if parent is None else f"{node.tag}{self._nth_of_type(node, parent)}"
            # Browsers insert the <tbody> that source HTML omits, so a source-built child
            # path would match nothing in the live DOM. Put it back.
            #
            # Known limitation: a table that *mixes* authored sections with bare <tr>s
            # (e.g. `<tbody>…</tbody><tr>…`) splits into two live <tbody> elements, and
            # only a tbody index tells them apart. That renumbering is skipped here, so
            # rows in such tables may share a selector. It only affects raw-source
            # parses; the browser paths parse page.content(), which is already normalised.
            if node.tag == "tr" and node.getparent().tag == "table":
                segment = f"tbody > {segment}"
            path = f"{path} > {segment}" if path else segment
            self._paths[node] = path
        return path

    def _nth_of_type(self, node: HtmlElement, parent: HtmlElement) -> str:
        """`:nth-of-type(n)` for a child, or "" when its tag is unique among siblings.

        Positions are indexed per parent, not rescanned per node — rescanning is
        O(n^2) on flat pages with thousands of same-tag siblings.
        """
        cache = self._siblings.get(parent)
        if cache is None:
            totals: Counter[str] = Counter()
            positions: dict[HtmlElement, int] = {}
            for child in parent:
                if isinstance(child.tag, str):
                    totals[child.tag] += 1
                    positions[child] = totals[child.tag]
            cache = self._siblings[parent] = (positions, totals)

        positions, totals = cache
        position = positions.get(node)
        if position is None or totals[node.tag] < 2:
            return ""
        return f":nth-of-type({position})"

    def _walk(
        self,
    ) -> tuple[list[tuple[HtmlElement, HtmlElement | None, HtmlElement | None]], dict[str, HtmlElement]]:
        """Single depth-first pass: prune, collect interactive nodes and labels.

        Skipping a pruned or hidden subtree outright is both the pruning
        rule and the fast path — nothing below it is ever visited.
        """
        found: list[tuple[HtmlElement, HtmlElement | None, HtmlElement | None]] = []
        labels_by_for: dict[str, HtmlElement] = {}
        # (element, nearest ancestor <label>, nearest ancestor card), pushed
        # reversed so pops stay in doc order.
        stack: list[tuple[HtmlElement, HtmlElement | None, HtmlElement | None]] = [
            (self.root, None, None)
        ]
        self.offscreen_skipped = 0
        self.occluded_skipped = 0

        while stack:
            el, ancestor_label, ancestor_card = stack.pop()
            if not isinstance(el.tag, str):  # comments, processing instructions
                continue
            # Hidden inputs aren't interactive — but they carry payload the agent
            # may need (CSRF tokens, per-view state), so they're collected into
            # hidden_fields before the prune rather than discarded wholesale.
            if el.tag == "input" and el.get("type") == "hidden":
                hidden = {
                    "name": el.get("name") or "",
                    "value": el.get("value") or "",
                    "selector": self._selector(el),
                }
                if el.get("id"):
                    hidden["id"] = el.get("id")
                if el.get("form"):
                    hidden["form"] = el.get("form")
                self.hidden_fields.append(hidden)
                continue
            # Skip zerodom's own injected UI (driving bar, sidebar, log panel).
            # SERIALIZE's in-page exclusion handles the primary path, but when
            # SERIALIZE returns null and page.content() is used as a fallback,
            # these elements survive into the raw HTML — so belt-and-suspenders
            # prune them here too.  data-zerodom-ignore is the explicit contract;
            # [id^="zerodom-"] catches any stray driving-bar sub-element that
            # might not carry the attribute.
            el_id = el.get("id") or ""
            if el_id.startswith("zerodom-") or el.get("data-zerodom-ignore") is not None:
                continue
            if (el.tag in PRUNE_TAGS and not is_shadow_root(el)) or self._is_hidden(el):
                continue
            if el.tag == "label":
                ancestor_label = el
                if for_id := el.get("for"):
                    labels_by_for.setdefault(for_id, el)
            if self._is_interactive(el):
                # Checked per-element, not as a subtree prune like _is_hidden:
                # an offscreen/occluded container can still have a
                # position:fixed/sticky child that IS onscreen and clickable
                # (a floating "back to top" button, a sticky filter bar), so
                # pruning the whole branch would hide it too.
                if self.viewport_only and self._is_offscreen(el):
                    self.offscreen_skipped += 1
                elif self.check_occlusion and self._is_occluded(el):
                    self.occluded_skipped += 1
                else:
                    found.append((el, ancestor_label, ancestor_card))
            # el's own card membership (just set above, if any) is the
            # context it was found IN, not itself — its children belong to
            # el's card only once el itself becomes that card, below.
            child_card = el if (self._is_card(el) or self._is_structural_card(el)) else ancestor_card
            stack.extend((child, ancestor_label, child_card) for child in reversed(el))
        return found, labels_by_for

    def _collapse_duplicate_nodes(self, nodes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """Collapse near-duplicate low-signal nodes.

        Groups nodes by (type, role, label). If a group has >=3 members
        and the nodes share no differentiating card/href/placeholder, collapse
        to one representative with a `count` annotation. This reduces token
        usage on feed pages with repeated controls like `button More actions`.
        """
        if not nodes:
            return nodes, 0
        from collections import defaultdict

        # Group by *index*, not by node, so the collapse can be applied in place
        # below. Rebuilding the list by walking the groups would emit every node
        # sharing a (type, role, label) together and silently reorder the whole
        # graph -- which fragments the @card runs in to_compact_text() (the same
        # card header repeats once per run, costing more tokens than the collapse
        # saves) and hands the model a node order that no longer matches the page.
        groups = defaultdict(list)
        for i, n in enumerate(nodes):
            groups[(n.get("type"), n.get("role"), n.get("label"))].append(i)

        drop: set[int] = set()
        counts: dict[int, int] = {}
        for idxs in groups.values():
            if len(idxs) < 3:
                continue
            # Collapse only when nothing differentiates the members: a shared
            # label is not enough if they sit in different cards or point
            # somewhere different.
            first = nodes[idxs[0]]
            if any(
                first.get(k) != nodes[j].get(k)
                for j in idxs[1:]
                for k in ("card", "href", "placeholder")
            ):
                continue
            counts[idxs[0]] = len(idxs)  # keep the first id, for selector stability
            drop.update(idxs[1:])

        collapsed = [
            {**n, "count": counts[i]} if i in counts else n
            for i, n in enumerate(nodes)
            if i not in drop
        ]
        return collapsed, len(drop)

    def parse(self) -> InteractionGraph:
        start = time.perf_counter()

        title_el = self.root.find(".//title")
        title = text_of(title_el) if title_el is not None else ""

        found, labels_by_for = self._walk()
        linker = LabelLinker(self.root, labels_by_for)
        card_titles: dict[HtmlElement, str] = {}
        # A card with only one interactive descendant has nothing to
        # disambiguate — that one node's own label already says what it is.
        # Grouping it anyway just adds a header line with no payoff, which is
        # exactly what a page of 200 one-link table rows doesn't need.
        card_counts = Counter(
            card for _, _, card in found if card is not None
        )

        for el, ancestor_label, ancestor_card in found:
            role = self._role(el)
            node: dict[str, Any] = {
                "id": f"node_{len(self.nodes) + 1:02d}",
                "type": el.tag,
                "role": role,
                "label": linker.label(el, ancestor_label),
                "selector": self._selector(el),
            }
            if ancestor_card is not None and card_counts[ancestor_card] >= 2:
                if ancestor_card not in card_titles:
                    card_titles[ancestor_card] = (
                        self._card_title(ancestor_card) or f"card {len(card_titles) + 1}"
                    )
                node["card"] = card_titles[ancestor_card]
            if el.tag == "input":
                node["input_type"] = el.get("type", "text")
            if placeholder := el.get("placeholder"):
                node["placeholder"] = placeholder
            if href := el.get("href"):
                node["href"] = href
            if "required" in el.attrib:
                node["required"] = True
            if "disabled" in el.attrib:
                node["disabled"] = True
            if self._is_content_editable(el):
                # mcp_server._act's signal to type via press_sequentially
                # (real per-character key events) instead of .fill()/.value= —
                # rich-text editors (Notion, Slack, Discord, Jira) run their own
                # state machine off real keystrokes and ignore or mishandle a
                # bulk insert.
                node["content_editable"] = True
            if role in FILLABLE_ROLES:
                node["value"] = el.get("value", "") if el.tag == "input" else (el.text or "")
                node["action"] = "fill"
            else:
                node["action"] = "click"
            self.nodes.append(node)

        dup_skipped = 0
        if self.collapse_duplicates:
            self.nodes, dup_skipped = self._collapse_duplicate_nodes(self.nodes)

        meta = {
            "page_title": title or "Untitled",
            "url": self.url,
            "total_interactive_nodes": len(self.nodes),
            "parsing_latency_ms": round((time.perf_counter() - start) * 1000, 2),
        }
        if self.offscreen_skipped:
            meta["offscreen_skipped"] = self.offscreen_skipped
        if self.occluded_skipped:
            meta["occluded_skipped"] = self.occluded_skipped
        if dup_skipped:
            meta["duplicates_collapsed"] = dup_skipped
        if self.hidden_fields:
            # Full list travels in the graph (verbose JSON), the count on the
            # same metadata the compact text previews — but values stay out of
            # the token-dense output a model reads at graph time; they're only
            # worth spending context on when a form actually needs them (see
            # mcp_server.zerodom_hidden_fields).
            meta["hidden_fields"] = list(self.hidden_fields)
            meta["hidden_field_count"] = len(self.hidden_fields)
        if warning := self._empty_page_warning(len(self.raw_html)):
            meta["warning"] = warning
        if blocked := _detect_challenge(self.raw_html):
            meta["blocked"] = blocked
        return InteractionGraph(nodes=self.nodes, metadata=meta)

    def _empty_page_warning(self, html_bytes: int) -> str | None:
        """Say *why* the graph looks empty, when it does.

        A bot wall, an open modal and a genuinely bare page all return almost
        nothing, and the caller cannot tell them apart from the node list. Every
        such case in a 49-site benchmark was one of these three, and each was
        reported as "ZeroDOM found nothing" until someone looked at the page.
        """
        if len(self.nodes) > 2:
            return None
        if html_bytes < 4000:
            return (
                "Almost no interactive nodes, and the page is tiny — this is "
                "usually a bot wall or an error page, not the site you wanted."
            )
        inert = self.root.xpath('//*[@aria-hidden="true" or @inert]//a')
        if inert:
            return (
                f"Almost no interactive nodes, but {len(inert)} links sit under "
                'aria-hidden/inert — a modal or overlay is open. Dismiss it and '
                "re-read."
            )
        return (
            "Almost no interactive nodes on a large page — the content is "
            "probably in an iframe (not traversed), a closed shadow root, or "
            "rendered to canvas. See Limitations."
        )


# Challenge/CAPTCHA fingerprints. In relay mode a hunter usually sails past these
# because the human already cleared the session; when one *is* in the way, the
# agent should hand off (or reuse a cleared session) instead of looping on it.
# Verified live 2026-09-23 against the vendor demo pages — see docs/PROFILE-A-PLAN.md.
_CHALLENGE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cloudflare", ("challenge-platform", "cf-chl", "__cf_chl", "cf-mitigated",
                    "/cdn-cgi/challenge", "checking if the site connection is secure")),
    ("turnstile", ("cf-turnstile", "challenges.cloudflare.com/turnstile", "turnstile/v0")),
    ("recaptcha", ("g-recaptcha", "grecaptcha", "recaptcha/api", "www.google.com/recaptcha")),
    ("hcaptcha", ("h-captcha", "hcaptcha.com")),
)


def _detect_challenge(html: str) -> dict[str, str] | None:
    """Which bot-wall/CAPTCHA a page carries, by fingerprint, or None.

    Deterministic substring match over the raw HTML — a marker is a marker
    wherever it sits (script src, inline config, class name). Cloudflare's
    full-page interstitial is checked first because it *blocks* the page, where a
    Turnstile/reCAPTCHA/hCaptcha widget usually sits on an otherwise usable form.
    """
    low = html.lower()
    for kind, markers in _CHALLENGE_MARKERS:
        for marker in markers:
            if marker in low:
                return {"kind": kind, "marker": marker}
    return None


def parse_html(
    html: str, url: str = "about:blank", viewport_only: bool = False, check_occlusion: bool = False, collapse_duplicates: bool = False
) -> InteractionGraph:
    """Convenience one-shot parse."""
    return ZeroDOMParser(html, url, viewport_only, check_occlusion, collapse_duplicates).parse()
