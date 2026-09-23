import time

import pytest

from zerodom import ZeroDOM, parse_html

LOGIN_FORM = """
<html><head>
  <title>Login - Example</title>
  <meta name="x" content="y"><link rel="stylesheet" href="a.css">
  <style>.a{color:red}</style><script>var x=1;</script>
</head><body>
  <svg><path d="M0 0"/></svg>
  <noscript>enable js</noscript>
  <form>
    <label for="email-input">Email Address</label>
    <input id="email-input" type="email" placeholder="user@example.com" required>

    <label>Password <input name="password" type="password"></label>

    <input id="token" type="hidden" value="csrf123">
    <input id="ghost" type="text" style="display: none">
    <div style="display:none"><input id="buried" type="text"><button>Buried</button></div>
    <div aria-hidden="true"><a href="/hidden">Hidden link</a></div>

    <input type="checkbox" aria-label="Remember me">
    <select name="country"><option>PK</option></select>
    <textarea aria-labelledby="notes-heading"></textarea>
    <h3 id="notes-heading">Notes</h3>

    <button id="submit-btn">Sign In</button>
    <div role="button" tabindex="0" class="fake-btn">Custom Action</div>
    <span onclick="go()">Clickable Span</span>
    <a href="/forgot">Forgot password?</a>
    <a name="anchor-only">not a link</a>
  </form>
</body></html>
"""


@pytest.fixture(scope="module")
def graph():
    return parse_html(LOGIN_FORM, url="https://example.com/login")


@pytest.fixture(scope="module")
def by_label(graph):
    return {n["label"]: n for n in graph["nodes"]}


# --- pruning -------------------------------------------------------

def test_bloat_and_hidden_nodes_are_pruned(graph):
    dumped = graph.to_json()
    for junk in ("stylesheet", "var x=1", "M0 0", "enable js"):
        assert junk not in dumped
    labels = {n["label"] for n in graph["nodes"]}
    assert not labels & {"Buried", "Hidden link"}
    assert not any(n["selector"] in {"#ghost", "#buried"} for n in graph["nodes"])
    # #token is a hidden input: its payload is captured separately (F10), kept
    # out of compact text, and not allowed to become a clickable node.
    assert not any(n["id"] == "node_??" and n["selector"] == "#token" for n in graph["nodes"])
    assert "csrf123" not in graph.to_compact_text()


def test_hidden_inputs_are_collected_not_pruned():
    html = (
        '<form><input type="hidden" name="csrf_token" value="abc123">'
        '<input type="hidden" id="draft_id" value="42">'
        '<input name="q"><button>Submit</button></form>'
    )
    graph = parse_html(html)
    # Not nodes — they're not clickable — but their payload survives for forms.
    assert [n["type"] for n in graph["nodes"]] == ["input", "button"]
    assert graph["metadata"]["hidden_field_count"] == 2
    assert graph["metadata"]["hidden_fields"] == [
        {"name": "csrf_token", "value": "abc123", "selector": "input[name='csrf_token']"},
        {"name": "", "value": "42", "selector": "#draft_id", "id": "draft_id"},
    ]
    # Values must never leak into the token-dense context an agent reads.
    compact = graph.to_compact_text()
    assert "abc123" not in compact and "42" not in compact


def test_controls_misparsed_into_head_are_still_found():
    """lxml keeps content after <title> in <head>; a browser moves it to <body>."""
    graph = parse_html("<title>T</title><button id='b'>Go</button>")
    assert [n["label"] for n in graph["nodes"]] == ["Go"]
    assert graph["metadata"]["page_title"] == "T"


def test_anchor_without_href_is_not_a_node(by_label):
    assert "not a link" not in by_label


# --- extraction & schema ---------------------------------

def test_all_visible_interactive_elements_are_captured(by_label):
    assert set(by_label) == {
        "Email Address", "Password", "Remember me", "country",
        "Notes", "Sign In", "Custom Action", "Clickable Span", "Forgot password?",
    }


def test_node_schema(by_label):
    email = by_label["Email Address"]
    assert email == {
        "id": "node_01",
        "type": "input",
        "role": "textbox",
        "label": "Email Address",
        "selector": "#email-input",
        "input_type": "email",
        "placeholder": "user@example.com",
        "required": True,
        "value": "",
        "action": "fill",
    }
    assert by_label["Sign In"] | {"action": "click", "selector": "#submit-btn"} == by_label["Sign In"]
    assert by_label["Remember me"]["role"] == "checkbox"
    assert by_label["country"]["role"] == "combobox"
    assert by_label["Forgot password?"]["href"] == "/forgot"


def test_metadata(graph):
    meta = graph["metadata"]
    assert meta["page_title"] == "Login - Example"
    assert meta["url"] == "https://example.com/login"
    assert meta["total_interactive_nodes"] == len(graph["nodes"]) == 9
    assert meta["parsing_latency_ms"] > 0


def test_ids_are_sequential(graph):
    assert [n["id"] for n in graph["nodes"]] == [f"node_{i:02d}" for i in range(1, 10)]


# --- label association ----------------------------------------------

def test_explicit_label_for(by_label):
    assert by_label["Email Address"]["selector"] == "#email-input"


def test_wrapping_label(by_label):
    assert by_label["Password"]["selector"] == "input[name='password']"


def test_aria_label_fallback(by_label):
    assert by_label["Remember me"]["input_type"] == "checkbox"


def test_aria_labelledby_fallback(by_label):
    assert by_label["Notes"]["type"] == "textarea"


def test_inner_text_fallback(by_label):
    assert by_label["Sign In"]["type"] == "button"
    assert by_label["Custom Action"]["role"] == "button"


@pytest.mark.parametrize(
    "html, expected",
    [
        # Parent's leading text — this is Hacker News' search form.
        ("<form>Search: <input name='q'></form>", "Search"),
        # Previous sibling's tail text.
        ("<div><b>x</b>Coupon code <input name='c'></div>", "Coupon code"),
        # Long copy before the input: keep the tail, not the sentence.
        ("<div>" + "word " * 30 + "Zip code: <input name='z'></div>", "word word word word Zip code"),
        # An authored attribute still wins over scraped text.
        ("<form>Search: <input name='q' aria-label='Site search'></form>", "Site search"),
        # Nothing adjacent: fall through to the identifier.
        ("<form><input name='q'></form>", "q"),
    ],
)
def test_adjacent_text_labels_captioned_inputs(html, expected):
    assert parse_html(html)["nodes"][0]["label"] == expected


def test_adjacent_text_does_not_hijack_buttons():
    # A button's own text is the label; neither the copy before it nor its name.
    html = "<div>Read the terms <button name='ok'>Accept</button></div>"
    assert parse_html(html)["nodes"][0]["label"] == "Accept"


def test_submit_input_prefers_its_value_over_adjacent_copy():
    html = "<form>Ready to go? <input type='submit' name='s' value='Send'></form>"
    assert parse_html(html)["nodes"][0]["label"] == "Send"


def test_select_is_not_labelled_by_its_first_option():
    html = "<select name='country'><option>PK</option><option>US</option></select>"
    assert parse_html(html)["nodes"][0]["label"] == "country"


def test_image_only_link_uses_img_alt():
    html = "<a href='/'><img src='logo.gif' alt='Hacker News'></a>"
    assert parse_html(html)["nodes"][0]["label"] == "Hacker News"


def test_unlabelled_input_falls_back_gracefully():
    nodes = parse_html("<input type='text'>")["nodes"]
    assert [n["label"] for n in nodes] == ["Unlabelled Element"]


def test_name_attribute_fallback(by_label):
    assert by_label["country"]["selector"] == "select[name='country']"


def test_file_input_is_a_button_not_a_textbox():
    """`page.fill()` throws on file inputs, so an agent must never be told to fill one."""
    node = parse_html("<form><input type='file' name='up'></form>")["nodes"][0]
    assert node["role"] == "button"
    assert node["action"] == "click"


# --- selectors ------------------------------------------------------------

def test_selector_disambiguates_identical_siblings():
    html = "<div><button>A</button></div><div><button>B</button><button>C</button></div>"
    selectors = [n["selector"] for n in parse_html(html)["nodes"]]
    # Paths carry their ancestors: a bare `button:nth-of-type(1)` would match the
    # first button under *every* div, and a bare `div` would match the first under
    # every parent — the chain runs all the way to the document root.
    assert selectors == [
        "body > div:nth-of-type(1) > button",
        "body > div:nth-of-type(2) > button:nth-of-type(1)",
        "body > div:nth-of-type(2) > button:nth-of-type(2)",
    ]


def test_nested_same_tag_does_not_alias():
    """Regression: with a descendant combinator this aimed HN's 'new' at the logo.

    `span a` matches the <a> nested two spans deep as well as the direct one, and
    :nth-of-type is omitted precisely because each tag is unique among its siblings.
    """
    html = ("<div id='row'><span><a href='/title'>Title</a>"
            "<span><a href='/site'>site.com</a></span></span></div>")
    selectors = [n["selector"] for n in parse_html(html)["nodes"]]
    assert selectors == ["#row > span > a", "#row > span > span > a"]
    assert len(set(selectors)) == 2


def test_table_rows_get_the_tbody_browsers_inject():
    html = "<table><tr><td><a href='/a'>A</a></td></tr></table>"
    assert parse_html(html)["nodes"][0]["selector"] == "body > table > tbody > tr > td > a"


def test_existing_tbody_is_not_duplicated():
    html = "<table><tbody><tr><td><a href='/a'>A</a></td></tr></tbody></table>"
    assert parse_html(html)["nodes"][0]["selector"] == "body > table > tbody > tr > td > a"


@pytest.mark.parametrize(
    "raw_id, expected",
    [
        ("email-input", "#email-input"),
        ("_x", "#_x"),
        ("49151933", '[id="49151933"]'),   # Hacker News numbers its rows
        ("3d.view", '[id="3d.view"]'),
        ("a b", '[id="a b"]'),
    ],
)
def test_numeric_and_odd_ids_use_attribute_selectors(raw_id, expected):
    """`#49151933` is a CSS parse error — querySelector throws rather than missing."""
    graph = parse_html(f"<button id='{raw_id}'>Go</button>")
    assert graph["nodes"][0]["selector"] == expected


def test_ancestor_anchor_also_escapes_odd_ids():
    html = "<div id='49151933'><span onclick='x()'>Go</span></div>"
    assert parse_html(html)["nodes"][0]["selector"] == '[id="49151933"] > span'


def test_paths_anchor_on_the_nearest_ancestor_id():
    html = "<div id='main'><section><span onclick='x()'>Go</span></section></div>"
    assert parse_html(html)["nodes"][0]["selector"] == "#main > section > span"


def test_unique_name_selector_is_kept():
    html = "<form><input name='q'><input type='hidden' name='x'></form>"
    assert parse_html(html)["nodes"][0]["selector"] == "input[name='q']"


def test_shared_name_falls_back_to_structural():
    """Radios/checkboxes/buttons in a group share a name — `tag[name='x']` would
    match every one of them, and :first-of-type clicking is a silent wrong action."""
    html = ("<form>"
            "<button name='save' onclick='a()'>Save</button>"
            "<button name='save' onclick='b()'>Save as</button>"
            "<input type='radio' name='c'><input type='radio' name='c'>"
            "</form>")
    selectors = [n["selector"] for n in parse_html(html)["nodes"]]
    assert len(set(selectors)) == 4
    assert all("name=" not in s for s in selectors)


def test_name_with_quote_is_css_escaped():
    html = "<form><input name=\"it's a test\"></form>"
    assert parse_html(html)["nodes"][0]["selector"] == "input[name='it\\'s a test']"


def test_duplicate_ids_fall_back_to_structural():
    """Duplicate ids are invalid HTML but common in real pages; `#dup` would match
    the first copy for every node inside the second."""
    html = ("<div id='dup'><a href='/1'>First</a></div>"
            "<div id='dup'><a href='/2'>Second</a></div>")
    selectors = [n["selector"] for n in parse_html(html)["nodes"]]
    assert len(set(selectors)) == 2
    assert all(s.startswith("body > div:nth-of-type") for s in selectors)


def test_duplicate_id_ancestor_is_not_anchored():
    html = ("<div id='dup'><span onclick='a()'>One</span></div>"
            "<div id='dup'><span onclick='b()'>Two</span></div>")
    selectors = [n["selector"] for n in parse_html(html)["nodes"]]
    assert len(set(selectors)) == 2
    assert all(s.startswith("body > div:nth-of-type") for s in selectors)


def test_root_anchored_paths_do_not_alias_across_subtrees():
    """Regression (MDN 'Skip to main content'): `ul > li:nth-of-type(1) > a`
    with html/body elided matched the first li > a of every ul on the page.
    The chain must run to the document root so the two walls can't collide."""
    html = ("<nav><ul><li><a href='/docs'>Docs</a></li></ul></nav>"
            "<footer><ul><li><a href='/about'>About</a></li></ul></footer>")
    selectors = [n["selector"] for n in parse_html(html)["nodes"]]
    assert len(set(selectors)) == 2
    assert all(s.startswith("body > ") for s in selectors)


def test_class_with_dot_uses_attribute_selector():
    """`div.a.b` reads as *two* classes and matches a different element."""
    html = ("<div class='a.b' onclick='x()'>Dotted</div>"
            "<div class='a b' onclick='y()'>Two classes</div>")
    selectors = [n["selector"] for n in parse_html(html)["nodes"]]
    assert selectors == ["div[class~='a.b']", "div.a"]
    assert len(set(selectors)) == 2


def test_class_starting_with_a_digit_is_escaped():
    """`.1x` is a CSS parse error — querySelector throws rather than missing."""
    html = "<div class='1x' onclick='x()'>Go</div>"
    assert parse_html(html)["nodes"][0]["selector"] == "div[class~='1x']"


# --- acceptance criteria --------------------------------------------------

def _timed(html: str) -> tuple[float, dict]:
    parse_html(html)  # warm import/JIT paths so the first call isn't charged
    start = time.perf_counter()
    graph = parse_html(html)
    return (time.perf_counter() - start) * 1000, graph


def test_latency_under_50ms_for_5000_nodes():
    """Typical page shape: 5k DOM nodes, a few hundred of them interactive."""
    row = "<div class='row'><span>text</span><em>x</em><b>y</b><a href='/x' id='l{i}'>link</a></div>"
    html = "<html><body>" + "".join(row.format(i=i) for i in range(1000)) + "</body></html>"
    elapsed, graph = _timed(html)
    assert graph["metadata"]["total_interactive_nodes"] == 1000
    assert elapsed < 50, f"{elapsed:.1f}ms"


def test_latency_worst_case_all_nodes_interactive_and_unselectable():
    """Every node interactive with no id/name/class, so all need structural paths."""
    html = "<html><body>" + "<div><a href='/x'>link</a><span>text</span></div>" * 1700 + "</body></html>"
    elapsed, graph = _timed(html)
    assert graph["metadata"]["total_interactive_nodes"] == 1700
    assert elapsed < 100, f"{elapsed:.1f}ms"


def test_compact_text_format(graph):
    lines = graph.to_compact_text().splitlines()
    assert lines[0] == "PAGE: Login - Example | https://example.com/login"
    assert lines[1] == "[01] input* 'Email Address' ph='user@example.com'"
    assert "[06] button 'Sign In'" in lines
    # The whole point: no CSS selector reaches the model.
    assert "#email-input" not in graph.to_compact_text()
    assert "#email-input" in graph.to_compact_text(selectors=True)


def test_selector_map_round_trips_every_node(graph):
    mapping = graph.selector_map()
    assert mapping["node_01"] == "#email-input"
    assert set(mapping) == {n["id"] for n in graph["nodes"]}
    # Every id printed in the compact graph resolves back to a selector.
    for line in graph.to_compact_text().splitlines()[1:]:
        index = line.split("]")[0].lstrip("[")
        assert f"node_{index}" in mapping


def test_compact_text_beats_json_on_link_dense_pages():
    tiktoken = pytest.importorskip("tiktoken")
    enc = tiktoken.get_encoding("cl100k_base")
    html = "<html><title>T</title><body><table>" + "".join(
        f"<tr><td><a href='/item?id={i}'>Story number {i}</a></td></tr>" for i in range(200)
    ) + "</table></body></html>"
    graph = parse_html(html)
    raw = len(enc.encode(html))
    assert len(enc.encode(graph.to_json(indent=None))) > raw  # JSON expands this page
    assert len(enc.encode(graph.to_compact_text())) < raw * 0.5


def test_token_savings_over_80_percent():
    tiktoken = pytest.importorskip("tiktoken")
    enc = tiktoken.get_encoding("cl100k_base")
    # Realistic bloat ratio: styling/scripts/markup around a handful of controls.
    html = LOGIN_FORM.replace("<body>", "<body>" + "<div class='wrapper flex px-4'><p>lorem ipsum dolor sit amet</p></div>" * 200)
    savings = 1 - len(enc.encode(parse_html(html).to_json())) / len(enc.encode(html))
    assert savings > 0.80


# --- Playwright adapter ---------------------------------------------

class FakeSyncPage:
    url = "https://example.com/login"

    def content(self):
        return LOGIN_FORM


class FakeAsyncPage:
    url = "https://example.com/login"

    async def content(self):
        return LOGIN_FORM


def test_from_page_sync():
    graph = ZeroDOM.from_page(FakeSyncPage())
    assert graph["metadata"]["url"] == "https://example.com/login"
    assert graph["metadata"]["total_interactive_nodes"] == 9
    assert '"nodes"' in graph.to_json()


def test_from_page_async():
    import asyncio

    graph = asyncio.run(ZeroDOM.from_page(FakeAsyncPage()))
    assert graph["metadata"]["total_interactive_nodes"] == 9


# --- declarative shadow roots -------------------------------------------------
#
# Chromium serializes open shadow roots as `<template shadowrootmode>`, and
# Playwright's CSS engine pierces them when it clicks. Both facts have to be modelled
# or a light-DOM selector silently matches elements the HTML never showed us.

SHADOW_PAGE = """<html><body>
  <div id="host"><button>Light</button><template shadowrootmode="open">
    <button>Shadow</button></template></div>
  <template><button>Inert</button></template>
</body></html>"""


def test_shadow_root_content_is_parsed():
    labels = [n["label"] for n in parse_html(SHADOW_PAGE)["nodes"]]
    assert "Shadow" in labels


def test_inert_template_is_still_pruned():
    labels = [n["label"] for n in parse_html(SHADOW_PAGE)["nodes"]]
    assert "Inert" not in labels


def test_colliding_light_and_shadow_children_get_distinct_selectors():
    """Regression: `#host > button` matches both trees, so it must not be emitted bare.

    Playwright counts `:nth-of-type` per tree, so the light and the shadow button are
    each "the first button under #host" — the exact silent-wrong-click this project
    treats as unacceptable.
    """
    nodes = {n["label"]: n["selector"] for n in parse_html(SHADOW_PAGE)["nodes"]}
    assert nodes["Light"] == ":light(#host > button)"
    assert nodes["Shadow"] == "#host > button >> nth=1"


def test_shadow_child_without_a_light_twin_keeps_a_plain_path():
    html = """<html><body><div id="card"><template shadowrootmode="open">
      <a href="/x">Only</a></template></div></body></html>"""
    assert parse_html(html)["nodes"][0]["selector"] == "#card > a"


def test_pages_without_shadow_roots_are_untouched():
    """The whole mechanism must cost nothing on the 99% of pages with no shadow DOM."""
    html = "<html><body><div id='d'><button>Go</button></div></body></html>"
    assert parse_html(html)["nodes"][0]["selector"] == "#d > button"


def test_icon_only_link_uses_descendant_title():
    """Hacker News' vote arrow: <a><div class="votearrow" title="upvote"></div></a>.

    The label lives on a descendant div, not on the <a> and not on an <img>.
    """
    graph = parse_html(
        "<a id='up_1' href='vote?id=1'><div class='votearrow' title='upvote'></div></a>"
    )
    assert graph["nodes"][0]["label"] == "upvote"


def test_image_link_without_alt_falls_back_to_href():
    """HN's logo is <a href="https://news.ycombinator.com"><img src="y18.svg"></a> —
    no text, no alt, no title. The destination is all that is left."""
    graph = parse_html(
        '<a href="https://news.ycombinator.com"><img src="y18.svg"></a>'
    )
    assert graph["nodes"][0]["label"] == "news.ycombinator.com"


def test_href_fallback_skips_uninformative_targets():
    for href in (
        "#",
        "javascript:void(0)",
        "JavaScript:void(0)",  # scheme check must be case-insensitive
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
    ):
        graph = parse_html(f'<a href="{href}"><img src="i.png"></a>')
        assert graph["nodes"][0]["label"] == "Unlabelled Element", href


def test_real_label_still_beats_href():
    graph = parse_html('<a href="https://example.com">Docs</a>')
    assert graph["nodes"][0]["label"] == "Docs"


def test_browser_marked_hidden_node_is_dropped():
    """The serializer marks what the CSS cascade hides; the parser must honour it."""
    graph = parse_html(
        "<button>Real</button>"
        "<input data-zerodom-hidden name='ghost'>"
    )
    assert [n["label"] for n in graph["nodes"]] == ["Real"]


def test_contenteditable_div_is_a_fillable_node():
    """Rich-text editors (Notion, Slack, Discord, Jira) edit through a
    <div contenteditable>, not an <input>/<textarea>."""
    graph = parse_html('<div contenteditable="true" id="composer">Type here</div>')
    node = graph["nodes"][0]
    assert node["action"] == "fill"
    assert node["role"] == "textbox"
    assert node["content_editable"] is True


def test_contenteditable_false_is_not_interactive():
    """`contenteditable="false"` carves an inert island — a mention chip
    inside an editable region — out, not a control in its own right."""
    graph = parse_html('<div contenteditable="false">static</div>')
    assert graph["nodes"] == []


def test_repeated_card_items_group_ambiguous_duplicate_labels():
    """Twenty identical 'Upvote' buttons are meaningless without knowing which
    story each belongs to — a <tr> (Hacker News's actual row markup: vote
    link + title link sharing one row) with 2+ controls gets a @card header
    naming it, so the model can tell them apart."""
    # Real Hacker News markup: the vote link carries no visible text (its
    # arrow is a CSS-styled <div>, not text) — only the title link does.
    html = (
        "<table>"
        "<tr><td><a href='/vote?id=1' title='upvote'><div class='votearrow'></div></a></td>"
        "<td><a href='/story?id=1'>Ask Academic mobile app</a></td></tr>"
        "<tr><td><a href='/vote?id=2' title='upvote'><div class='votearrow'></div></a></td>"
        "<td><a href='/story?id=2'>Recommended image viewer for Arch</a></td></tr>"
        "</table>"
    )
    graph = parse_html(html)
    assert [n.get("card") for n in graph["nodes"]] == [
        "Ask Academic mobile app", "Ask Academic mobile app",
        "Recommended image viewer for Arch", "Recommended image viewer for Arch",
    ]
    text = graph.to_compact_text()
    assert "@card 'Ask Academic mobile app':\n  [01] a 'upvote'" in text
    assert "@card 'Recommended image viewer for Arch':\n  [03] a 'upvote'" in text


def test_card_with_only_one_control_is_not_grouped():
    """Nothing to disambiguate with a single control in the row — grouping it
    anyway is a header with no payoff (caught live: it doubled the line count
    on a 200-row single-link-per-row benchmark fixture for zero benefit)."""
    graph = parse_html("<table><tr><td><a href='/x'>Story number 0</a></td></tr></table>")
    assert graph["nodes"][0].get("card") is None
    assert "@card" not in graph.to_compact_text()


def test_untitled_card_falls_back_to_positional_numbering():
    """No heading, no link text, no visible text anywhere in the row (icon-only
    buttons labelled via aria-label) — nothing to name the card after."""
    html = (
        "<table><tr><button aria-label='A'></button><button aria-label='B'></button></tr>"
        "<tr><button aria-label='C'></button><button aria-label='D'></button></tr></table>"
    )
    graph = parse_html(html)
    assert [n["card"] for n in graph["nodes"]] == ["card 1", "card 1", "card 2", "card 2"]


def test_shadcn_style_div_cards_group_via_structural_sibling_match():
    """Component-library dashboards (Salesforce, Jira-style admin UIs,
    Shadcn/Radix/Tailwind) build repeating cards as plain <div>s with no
    semantic tag or role — three-plus siblings sharing tag+class is the
    fallback signal."""
    card = (
        "<div class='rounded-lg border bg-card p-6'>"
        "<h3>{title}</h3><button>Edit</button><button>Delete</button></div>"
    )
    html = "<div>" + "".join(card.format(title=f"Widget {i}") for i in range(3)) + "</div>"
    graph = parse_html(html)
    assert [n["card"] for n in graph["nodes"]] == [
        "Widget 0", "Widget 0", "Widget 1", "Widget 1", "Widget 2", "Widget 2",
    ]


def test_two_similar_divs_are_not_enough_to_count_as_structural_siblings():
    """Below the 3-sibling threshold, a div/class match alone isn't a strong
    enough signal — real pages have all kinds of paired layout divs that
    aren't repeated-item cards."""
    card = "<div class='card'><button>A</button><button>B</button></div>"
    html = card + card
    graph = parse_html(html)
    assert all(n.get("card") is None for n in graph["nodes"])


def test_card_title_is_clamped_tighter_than_a_node_labels_max_length():
    """A card title is repeated overhead (one line per card), so it gets a
    tighter cap than a node label's own 80-char MAX_LABEL_LEN."""
    long_heading = "This heading is way too long to repeat as a header on every single row " * 2
    html = (
        f"<table><tr><td><h3>{long_heading}</h3></td>"
        "<td><button>A</button><button>B</button></td></tr>"
        f"<tr><td><h3>{long_heading}</h3></td>"
        "<td><button>C</button><button>D</button></td></tr></table>"
    )
    graph = parse_html(html)
    title = graph["nodes"][0]["card"]
    assert len(title) <= 40
    assert title.endswith("…")


def test_browser_marked_offscreen_node_is_dropped_only_when_requested():
    """viewport_only=True drops what the serializer marked off-screen; off by
    default so a page's node count never changes unless a caller opts in."""
    html = "<button>Onscreen</button><button data-zerodom-offscreen>Below fold</button>"
    assert [n["label"] for n in parse_html(html)["nodes"]] == ["Onscreen", "Below fold"]
    graph = parse_html(html, viewport_only=True)
    assert [n["label"] for n in graph["nodes"]] == ["Onscreen"]
    assert graph["metadata"]["offscreen_skipped"] == 1
    assert "1 more nodes offscreen" in graph.to_compact_text()


def test_browser_marked_occluded_node_is_dropped_only_when_requested():
    """check_occlusion=True drops what the serializer marked as covered by
    something else (a modal backdrop, an open dropdown); off by default —
    an elementFromPoint() hit-test per node is real, unmeasured cost."""
    html = "<button>Reachable</button><button data-zerodom-occluded>Behind modal</button>"
    assert [n["label"] for n in parse_html(html)["nodes"]] == ["Reachable", "Behind modal"]
    graph = parse_html(html, check_occlusion=True)
    assert [n["label"] for n in graph["nodes"]] == ["Reachable"]
    assert graph["metadata"]["occluded_skipped"] == 1
    assert "1 nodes hidden behind an overlay" in graph.to_compact_text()


def test_empty_fragment_anchors_are_not_controls():
    """`<a href="#x" id="x"></a>` is a link destination, not a link.

    GitHub-rendered markdown emits one per heading; nine of them showed up on
    PyPI's own project page, all unlabelled and all unclickable.
    """
    graph = parse_html(
        '<a href="#install" id="user-content-install"></a>'
        '<a href="#install">Install</a>'
    )
    assert [n["label"] for n in graph["nodes"]] == ["Install"]


def test_fragment_anchor_with_content_is_still_a_control():
    """A fragment link the user can actually see and click must survive."""
    graph = parse_html('<a href="#top"><img src="up.png" alt="Back to top"></a>')
    assert [n["label"] for n in graph["nodes"]] == ["Back to top"]


def test_svg_title_labels_an_icon_button():
    graph = parse_html('<button><svg><title>Delete row</title></svg></button>')
    assert graph["nodes"][0]["label"] == "Delete row"


def test_tooltip_attribute_labels_an_icon_link():
    """jsfiddle's toolbar: the caption lives only in the tooltip library's attr."""
    graph = parse_html(
        '<a id="save" href="" data-tippy-simple-content="Save fiddle"><svg></svg></a>'
    )
    assert graph["nodes"][0]["label"] == "Save fiddle"


def test_bot_wall_is_reported_not_silently_empty():
    """A blocked page and a bare page look identical in the node list."""
    graph = parse_html("<html><body></body></html>", "https://etsy.com")
    assert graph["nodes"] == []
    assert "bot wall" in graph["metadata"]["warning"]


def test_open_modal_is_named_as_the_reason():
    """Airbnb: 255 of 257 controls under aria-hidden because a modal is open."""
    html = (
        "<html><body>"
        "<div aria-hidden='true'>" + "<a href='/x'>hidden</a>" * 40 + "</div>"
        "<div role='dialog'><button>Got it</button></div>"
        "</body></html>"
    )
    graph = parse_html(html + "<!--" + "p" * 5000 + "-->")
    assert [n["label"] for n in graph["nodes"]] == ["Got it"]
    assert "modal or overlay is open" in graph["metadata"]["warning"]


def test_healthy_page_carries_no_warning():
    graph = parse_html("<a href='/a'>A</a><a href='/b'>B</a><a href='/c'>C</a>")
    assert "warning" not in graph["metadata"]


def test_handler_name_labels_a_bare_icon_div():
    """jsfiddle's ad-close button: an SVG path in a div, named only by its handler."""
    graph = parse_html(
        """<div onclick="_bsa.close('_stickybox_')" tabindex="0"><svg><path d="M7 4"/></svg></div>"""
    )
    assert graph["nodes"][0]["label"] == "close"


def test_handler_label_splits_camel_case():
    graph = parse_html('<div onclick="toggleSidebarMenu()" tabindex="0"></div>')
    assert graph["nodes"][0]["label"] == "toggle sidebar menu"


def test_handler_label_refuses_plumbing():
    for js in ("return false", "event.preventDefault()", "setTimeout(f, 10)"):
        graph = parse_html(f'<div onclick="{js}" tabindex="0"></div>')
        assert graph["nodes"][0]["label"] == "Unlabelled Element", js


def test_real_text_still_beats_the_handler_name():
    graph = parse_html('<button onclick="doStuff()">Save changes</button>')
    assert graph["nodes"][0]["label"] == "Save changes"


def test_tabindex_negative_one_is_not_interactive():
    """tabindex="-1" means "focusable via .focus() only, not part of the
    keyboard tab order" per the HTML spec — a standard focus-management
    pattern (modals, route-change targets), never a real control. Confirmed
    live on google.com/maps: <body tabindex="-1"> was misclassified as a
    clickable node this way, with its label falling through to raw
    <script> text (see the label_linker test below)."""
    graph = parse_html('<div tabindex="-1">Not a real control</div>')
    assert graph["nodes"] == []


def test_tabindex_zero_and_positive_are_still_interactive():
    for value in ("0", "1", "5"):
        graph = parse_html(f'<div tabindex="{value}">Real control</div>')
        assert len(graph["nodes"]) == 1, value
        assert graph["nodes"][0]["label"] == "Real control"


def test_tabindex_garbage_value_is_not_interactive():
    graph = parse_html('<div tabindex="not-a-number">Junk</div>')
    assert graph["nodes"] == []


def test_tabindex_negative_one_wrapper_does_not_shadow_its_real_children():
    """The exact google.com/maps shape: a tabindex="-1" wrapper around
    several real buttons used to show up as its own node with all its
    children's text mashed together, on top of the (correct) individual
    button nodes."""
    graph = parse_html(
        '<div tabindex="-1">'
        '<button>Restaurants</button><button>Hotels</button>'
        "</div>"
    )
    labels = [n["label"] for n in graph["nodes"]]
    assert labels == ["Restaurants", "Hotels"]


def test_label_never_picks_up_a_nested_scripts_source_as_text():
    """el.text_content() concatenates a <script> descendant's raw JS right
    along with real visible text — confirmed live on google.com/maps, where
    a real onclick button whose fallback label came from text_of() picked up
    an inline <script>'s source instead of anything a person would read."""
    graph = parse_html(
        '<button onclick="f()">Real label'
        '<script>tick(\'b0\');if (x > 1) { y() }</script>'
        "</button>"
    )
    assert graph["nodes"][0]["label"] == "Real label"


def test_label_never_picks_up_a_nested_styles_source_as_text():
    graph = parse_html(
        '<button onclick="f()">Real label<style>.x{color:red}</style></button>'
    )
    assert graph["nodes"][0]["label"] == "Real label"


def test_label_strips_icon_font_private_use_area_glyphs():
    """Icon fonts render a glyph at a Private Use Area codepoint
    (U+E000-F8FF) glued directly onto the real word with no separator —
    confirmed live on google.com/maps: text_content() on a "Restaurants"
    button came back as the icon codepoint immediately followed by the word."""
    icon = chr(0xE56C)
    graph = parse_html(f'<button onclick="f()">{icon}Restaurants</button>')
    assert graph["nodes"][0]["label"] == "Restaurants"


def test_label_never_picks_up_an_ordinary_html_comment_as_text():
    """HTML comments are invisible by definition — no browser ever renders
    one — but lxml's Comment nodes carry their text on `.text` like a real
    element, so a naive text_content()-style walk concatenates it right in.
    Not framework-specific: a plain `<!-- normal comment -->` leaks the
    exact same way."""
    graph = parse_html('<button onclick="f()"><!-- normal comment -->Join</button>')
    assert graph["nodes"][0]["label"] == "Join"


def test_label_never_picks_up_a_lit_hydration_marker_comment_as_text():
    """Confirmed live on reddit.com: Lit-based shreddit-* web components
    leave declarative-shadow-DOM hydration marker comments
    (<!--?lit$438023304$-->) in the DOM, and they concatenated straight
    into real button labels ("?lit$438023304$Join")."""
    graph = parse_html('<button onclick="f()"><!--?lit$438023304$-->Join</button>')
    assert graph["nodes"][0]["label"] == "Join"


def test_label_keeps_real_text_immediately_after_a_comment():
    """The fix must skip only the comment's own text, not the real text
    node that follows it in the same parent."""
    graph = parse_html('<button onclick="f()">Real<!-- marker --> Label</button>')
    assert graph["nodes"][0]["label"] == "Real Label"


def test_duplicate_node_collapse():
    """Three identical buttons should collapse to one entry with count when enabled."""
    html = "<button>More</button><button>More</button><button>More</button>"
    graph = parse_html(html, collapse_duplicates=True)
    assert len(graph["nodes"]) == 1
    node = graph["nodes"][0]
    assert node["label"] == "More"
    assert node.get("count") == 3
    assert graph["metadata"].get("duplicates_collapsed") == 2
    # compact text shows the × count
    assert "×3" in graph.to_compact_text()



def test_collapsing_duplicates_keeps_document_order():
    """collapse_duplicates must not reorder the graph.

    It used to rebuild the node list by walking the (type, role, label) groups,
    which emitted every same-labelled node together. On a feed that fragmented
    the @card runs in to_compact_text() -- the same card header repeated once
    per run, costing *more* tokens than the collapse saved -- and handed the
    model a node order that no longer matched the page.
    """
    html = "<html><body>" + "".join(
        f'<li><a href="/p{i}">Post {i}</a><a href="/like/{i}">Like</a>'
        f'<a href="/share/{i}">Share</a></li>'
        for i in range(1, 4)
    ) + "</body></html>"

    graph = parse_html(html, "https://e.com", collapse_duplicates=True)
    labels = [n["label"] for n in graph["nodes"]]
    assert labels == [
        "Post 1", "Like", "Share",
        "Post 2", "Like", "Share",
        "Post 3", "Like", "Share",
    ]

    text = graph.to_compact_text()
    cards = [ln for ln in text.splitlines() if ln.startswith("@card")]
    assert len(cards) == len(set(cards)) == 3, f"card runs fragmented:\n{text}"


def test_collapsing_duplicates_still_collapses_undifferentiated_repeats():
    """The ordering fix must not cost the feature its actual job."""
    html = "<html><body>" + "".join(
        '<div><button>More actions</button></div>' for _ in range(5)
    ) + "</body></html>"

    graph = parse_html(html, "https://e.com", collapse_duplicates=True)
    assert len(graph["nodes"]) == 1
    assert graph["nodes"][0]["count"] == 5
    assert graph["metadata"]["duplicates_collapsed"] == 4
