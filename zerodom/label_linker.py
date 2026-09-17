"""Work out what a control is called, the way a person reading the page would."""

from __future__ import annotations

import re

from lxml.html import HtmlElement

# Attribute fallbacks, most explicit first. Split around the adjacent-text
# scan: authored attributes beat scraped text, but scraped text beats `name`/`value`,
# which are developer identifiers ("q") rather than anything a user reads.
AUTHORED_ATTRS = ("aria-label", "placeholder", "alt", "title")
IDENTIFIER_ATTRS = ("value", "name")

# Icon buttons routinely put their only human-readable caption in a tooltip
# library's data attribute. These four cover Tippy, Bootstrap and the
# conventional hand-rolled spelling, which between them are most of the web.
TOOLTIP_ATTRS = (
    "data-tooltip",
    "data-original-title",
    "data-tippy-content",
    "data-tippy-simple-content",
)

# Controls that sit next to their caption. Buttons and links carry their own text.
CAPTIONED_TAGS = {"input", "select", "textarea"}

# Inputs whose `value` is a button caption rather than user data.
BUTTON_INPUT_TYPES = {"submit", "button", "reset", "image"}

MAX_LABEL_LEN = 80

# Trailing punctuation on a caption: "Search:" -> "Search".
CAPTION_STRIP = " \t\n\xa0:*>-–—"


def text_of(el: HtmlElement) -> str:
    """Visible text of an element, whitespace-collapsed and length-capped."""
    return " ".join(el.text_content().split())[:MAX_LABEL_LEN]


def adjacent_text(el: HtmlElement) -> str:
    """Caption sitting immediately before an input, as in `Search: <input>`.

    Reads the previous sibling's tail, or the parent's leading text when the input
    is the first child. Only the last few words are kept — the text run before an
    input is often a whole sentence of surrounding copy, not a caption.
    """
    parent = el.getparent()
    if parent is None:
        return ""
    previous = el.getprevious()
    source = previous.tail if previous is not None else parent.text
    caption = " ".join((source or "").split()).rstrip(CAPTION_STRIP)
    if not caption:
        return ""
    return " ".join(caption.split()[-6:])[:MAX_LABEL_LEN]


# Handler names that describe the plumbing rather than the action.
_HANDLER_NOISE = {
    "return", "function", "void", "this", "event", "typeof", "new", "if",
    "settimeout", "requestanimationframe", "preventdefault", "stoppropagation",
}
_HANDLER_CALL = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
_WORD_BREAK = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _handler_label(onclick: str) -> str:
    """The action an inline handler names, as words. `_bsa.close(…)` -> "close".

    Only ever reached when an element has no text, no icon and no attribute of
    its own, which in practice means an icon `<div onclick>` built out of an SVG
    path. Conservative on purpose: a name that describes the wiring instead of
    the intent is worse than admitting the control is unlabelled.
    """
    for name in reversed(_HANDLER_CALL.findall(onclick or "")):
        if len(name) < 3 or name.lower().lstrip("_") in _HANDLER_NOISE:
            continue
        words = _WORD_BREAK.sub(" ", name.strip("_$")).replace("_", " ").replace("-", " ")
        if cleaned := " ".join(words.split()).lower():
            return cleaned[:MAX_LABEL_LEN]
    return ""


def _href_label(href: str) -> str:
    """A link's destination, trimmed to the part a person would read aloud.

    Last resort only — a real caption always wins. `javascript:` and bare
    fragments say nothing about where the link goes, so they stay unlabelled.
    """
    href = href.strip()
    if href.lower().startswith(("javascript:", "data:", "vbscript:", "#")) or not href:
        return ""
    # Drop scheme and www so "https://news.ycombinator.com/" reads as the site.
    trimmed = re.sub(r"^[a-z][a-z0-9+.-]*://(www\.)?", "", href, flags=re.I)
    return trimmed.rstrip("/")[:MAX_LABEL_LEN]


class LabelLinker:
    """Resolves labels against a pre-indexed set of <label> tags.

    The parser walks the DOM once and hands over what it saw, so label lookup
    stays O(1) per node instead of re-searching the tree for every input.
    """

    def __init__(
        self, root: HtmlElement, labels_by_for: dict[str, HtmlElement] | None = None
    ) -> None:
        self.root = root
        if labels_by_for is None:
            labels_by_for = {}
            for tag in root.iter("label"):
                if (for_id := tag.get("for")) and for_id not in labels_by_for:
                    labels_by_for[for_id] = tag
        self.labels_by_for = labels_by_for

    def label(self, element: HtmlElement, ancestor_label: HtmlElement | None = None) -> str:
        """Resolve a human-readable label for an interactive element."""
        # explicit <label for="...">
        elem_id = element.get("id")
        if elem_id and (tag := self.labels_by_for.get(elem_id)) is not None:
            if text := text_of(tag):
                return text

        # wrapping <label>
        if ancestor_label is None:
            ancestor_label = next(element.iterancestors("label"), None)
        if ancestor_label is not None and (text := text_of(ancestor_label)):
            return text

        # aria-labelledby -> referenced element's text
        for ref_id in element.get("aria-labelledby", "").split():
            refs = self.root.xpath("//*[@id=$ref]", ref=ref_id)
            if refs and (text := text_of(refs[0])):
                return text

        # authored attribute fallbacks
        for attr in AUTHORED_ATTRS:
            if (val := element.get(attr)) and val.strip():
                return val.strip()[:MAX_LABEL_LEN]

        # On a submit/button input, `value` is the visible caption, not an identifier.
        if element.tag == "input" and element.get("type") in BUTTON_INPUT_TYPES:
            if (val := element.get("value")) and val.strip():
                return val.strip()[:MAX_LABEL_LEN]

        # Caption text next to the control, before falling back to identifiers
        if element.tag in CAPTIONED_TAGS and (caption := adjacent_text(element)):
            return caption

        # Inner text (buttons, links). Skipped for captioned controls, whose contents
        # are their value or options — a <select>'s first <option> is not its label.
        if element.tag not in CAPTIONED_TAGS and (text := text_of(element)):
            return text

        for attr in IDENTIFIER_ATTRS:
            if (val := element.get(attr)) and val.strip():
                return val.strip()[:MAX_LABEL_LEN]

        # Icon-only controls carry their label on a descendant, not on themselves:
        # an <img alt>, but just as often a styled <div title> or <i aria-label>.
        # Hacker News' vote arrows are <a><div class="votearrow" title="upvote"></div></a>,
        # and they are the most-clicked control on the page.
        for descendant in element.iterdescendants():
            for attr in AUTHORED_ATTRS:
                if (val := descendant.get(attr)) and val.strip():
                    return val.strip()[:MAX_LABEL_LEN]
            # <svg><title>Save</title></svg> is the standards-defined accessible
            # name for an inline icon. The walk prunes <svg>, but the label
            # lookup reads it directly.
            if descendant.tag in ("title", "desc") and (text := text_of(descendant)):
                return text

        # Tooltip libraries, checked after real markup and before giving up.
        for attr in TOOLTIP_ATTRS:
            if (val := element.get(attr)) and val.strip():
                return val.strip()[:MAX_LABEL_LEN]

        # A link with no text and no icon label still has somewhere to go, and the
        # destination is the only thing left that tells an agent them apart.
        if element.tag == "a" and (href := (element.get("href") or "").strip()):
            if label := _href_label(href):
                return label

        # Last resort: the handler's own name. A bare `<div onclick="_bsa.close(…)">`
        # wrapping an SVG path has no text, no alt, no title and no tooltip — but
        # somebody named the function, and "close" is worth more to an agent than
        # "Unlabelled Element".
        if handler := _handler_label(element.get("onclick", "")):
            return handler

        return "Unlabelled Element"


def get_label(root: HtmlElement, element: HtmlElement) -> str:
    """Standalone label lookup for a single element."""
    return LabelLinker(root).label(element)
