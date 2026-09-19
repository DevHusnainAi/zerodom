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

        `*` marks required, `!` marks disabled.
        """
        meta = self["metadata"]
        lines = [f"PAGE: {meta['page_title']} | {meta['url']}"]
        lines += [compact_line(node, selectors, hrefs) for node in self["nodes"]]
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
    if placeholder := node.get("placeholder"):
        line += f" ph={placeholder!r}"
    if hrefs and (href := node.get("href")):
        line += f" -> {href}"
    if selectors:
        line += f" ({node['selector']})"
    return line


class ZeroDOMParser:
    """Parses raw HTML into a token-optimized Interaction Graph."""

    def __init__(self, html: str, url: str = "about:blank") -> None:
        self.raw_html = html
        self.url = url
        self.root: HtmlElement = lxml.html.document_fromstring(html.strip() or "<html></html>")
        self.nodes: list[dict[str, Any]] = []
        # (kind, ...) -> occurrences, one pass over the tree, keyed so each kind of
        # uniqueness claim — id, (tag, name), (tag, class) — is counted separately.
        self._occ: Counter[tuple[str, ...]] | None = None
        # parent element -> (child -> nth-of-type position, tag -> sibling count)
        self._siblings: dict[HtmlElement, tuple[dict[HtmlElement, int], Counter[str]]] = {}
        self._paths: dict[HtmlElement, str] = {}
        self._shadow: bool | None = None

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
    def _is_interactive(el: HtmlElement) -> bool:
        """Is this something an agent can act on?"""
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

    def _walk(self) -> tuple[list[tuple[HtmlElement, HtmlElement | None]], dict[str, HtmlElement]]:
        """Single depth-first pass: prune, collect interactive nodes and labels.

        Skipping a pruned or hidden subtree outright is both the pruning
        rule and the fast path — nothing below it is ever visited.
        """
        found: list[tuple[HtmlElement, HtmlElement | None]] = []
        labels_by_for: dict[str, HtmlElement] = {}
        # (element, nearest ancestor <label>), pushed reversed so pops stay in doc order.
        stack: list[tuple[HtmlElement, HtmlElement | None]] = [(self.root, None)]

        while stack:
            el, ancestor_label = stack.pop()
            if not isinstance(el.tag, str):  # comments, processing instructions
                continue
            if (el.tag in PRUNE_TAGS and not is_shadow_root(el)) or self._is_hidden(el):
                continue
            if el.tag == "label":
                ancestor_label = el
                if for_id := el.get("for"):
                    labels_by_for.setdefault(for_id, el)
            if self._is_interactive(el):
                found.append((el, ancestor_label))
            stack.extend((child, ancestor_label) for child in reversed(el))
        return found, labels_by_for

    def parse(self) -> InteractionGraph:
        start = time.perf_counter()

        title_el = self.root.find(".//title")
        title = text_of(title_el) if title_el is not None else ""

        found, labels_by_for = self._walk()
        linker = LabelLinker(self.root, labels_by_for)

        for el, ancestor_label in found:
            role = self._role(el)
            node: dict[str, Any] = {
                "id": f"node_{len(self.nodes) + 1:02d}",
                "type": el.tag,
                "role": role,
                "label": linker.label(el, ancestor_label),
                "selector": self._selector(el),
            }
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
            if role in FILLABLE_ROLES:
                node["value"] = el.get("value", "") if el.tag == "input" else (el.text or "")
                node["action"] = "fill"
            else:
                node["action"] = "click"
            self.nodes.append(node)

        meta = {
            "page_title": title or "Untitled",
            "url": self.url,
            "total_interactive_nodes": len(self.nodes),
            "parsing_latency_ms": round((time.perf_counter() - start) * 1000, 2),
        }
        if warning := self._empty_page_warning(len(self.raw_html)):
            meta["warning"] = warning
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


def parse_html(html: str, url: str = "about:blank") -> InteractionGraph:
    """Convenience one-shot parse."""
    return ZeroDOMParser(html, url).parse()
