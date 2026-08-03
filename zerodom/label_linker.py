"""Work out what a control is called, the way a person reading the page would."""

from __future__ import annotations

from lxml.html import HtmlElement

# Attribute fallbacks, most explicit first. Split around the adjacent-text
# scan: authored attributes beat scraped text, but scraped text beats `name`/`value`,
# which are developer identifiers ("q") rather than anything a user reads.
AUTHORED_ATTRS = ("aria-label", "placeholder", "alt", "title")
IDENTIFIER_ATTRS = ("value", "name")

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

        # Image-only links and buttons carry their label on the <img>.
        for img in element.iterdescendants("img"):
            if alt := (img.get("alt") or img.get("title") or "").strip():
                return alt[:MAX_LABEL_LEN]

        return "Unlabelled Element"


def get_label(root: HtmlElement, element: HtmlElement) -> str:
    """Standalone label lookup for a single element."""
    return LabelLinker(root).label(element)
