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
    for junk in ("csrf123", "stylesheet", "var x=1", "M0 0", "enable js"):
        assert junk not in dumped
    labels = {n["label"] for n in graph["nodes"]}
    assert not labels & {"Buried", "Hidden link"}
    assert not any(n["selector"] in {"#ghost", "#buried"} for n in graph["nodes"])


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
