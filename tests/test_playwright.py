"""Real-browser integration. Skipped unless `playwright install chromium` has run."""

from pathlib import Path

import pytest

from zerodom import ZeroDOM

pytestmark = pytest.mark.skipif(
    not list(Path.home().glob(".cache/ms-playwright/chromium*")),
    reason="chromium not installed (run: uv run playwright install chromium)",
)

PAGE = """
<html><head><title>Login</title><style>.x{color:red}</style></head><body>
  <script>window.x = 1</script>
  <form>
    <label for="email">Email</label><input id="email" type="email" required>
    <div style="display:none"><input id="ghost"></div>
    <button id="submit">Sign In</button>
  </form>
  <!-- no id/name/class anywhere: forces the structural selector path -->
  <nav><div><a href="/a">A</a></div><div><a href="/b">B</a><a href="/c">C</a></div></nav>
  <!-- the browser injects <tbody> here; selectors must survive it -->
  <table><tr><td><a href="/t1">T1</a></td></tr><tr><td><a href="/t2">T2</a></td></tr></table>
  <!-- Hacker News' shape: a nested <a> that a descendant path would also match -->
  <div id="row"><span><a href="/story">Story</a><span><a href="/site">site.com</a></span></span></div>
</body></html>
"""


@pytest.fixture(scope="module")
def browser():
    # One browser for the module: the sync API cannot start inside a running
    # asyncio loop, and other test modules leave one behind.
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="module")
def page(browser):
    p = browser.new_page()
    p.set_content(PAGE)
    yield p
    p.close()


def test_from_page_round_trip(page):
    graph = ZeroDOM.from_page(page)
    nodes = {n["label"]: n for n in graph["nodes"]}
    assert set(nodes) == {"Email", "Sign In", "A", "B", "C", "T1", "T2", "Story", "site.com"}
    assert graph["metadata"]["page_title"] == "Login"
    assert "window.x" not in graph.to_json()


def test_every_selector_is_unique_in_the_real_browser(page):
    """The structural fallback must resolve to exactly one element in Chromium."""
    for node in ZeroDOM.from_page(page)["nodes"]:
        assert page.locator(node["selector"]).count() == 1, node


def test_fill_targets_the_right_element(page):
    for node in ZeroDOM.from_page(page)["nodes"]:
        if node["action"] == "fill":
            page.fill(node["selector"], "a@b.co")
    assert page.input_value("#email") == "a@b.co"


def test_structural_selector_picks_the_right_sibling(page):
    labels = {n["label"]: n["selector"] for n in ZeroDOM.from_page(page)["nodes"]}
    for label in ("A", "B", "C", "T1", "T2"):
        assert page.locator(labels[label]).inner_text() == label


def test_name_groups_and_duplicate_ids_resolve_uniquely(browser):
    """Regression: `input[name='c']` matched every radio in the group and `#dup`
    every copy of a duplicated id — both silent wrong clicks. Each selector must
    resolve to exactly one element, and clicking must hit the right one."""
    html = """<html><body>
      <form>
        <button type="button" name="save" onclick="mark('first')">First</button>
        <button type="button" name="save" onclick="mark('second')">Second</button>
        <input type="radio" name="c" value="a"><input type="radio" name="c" value="b">
        <input type="text" name="it's a test">
      </form>
      <div id="dup"><span onclick="mark('left')">Left</span></div>
      <div id="dup"><span onclick="mark('right')">Right</span></div>
      <script>window.hits=[];function mark(v){window.hits.push(v)}</script>
    </body></html>"""
    p = browser.new_page()
    p.set_content(html)
    p.evaluate("window.hits = []")
    nodes = ZeroDOM.from_page(p)["nodes"]
    by_label = {n["label"]: n for n in nodes}

    # Every selector resolves to exactly one element.
    assert all(p.locator(n["selector"]).count() == 1 for n in nodes), [
        n["selector"] for n in nodes if p.locator(n["selector"]).count() != 1
    ]

    # Same-named buttons click the one the label points at.
    p.click(by_label["Second"]["selector"])
    assert p.evaluate("window.hits") == ["second"]

    # Duplicate-id span clicks its own copy, not the first.
    p.click(by_label["Right"]["selector"])
    assert p.evaluate("window.hits") == ["second", "right"]

    # A name containing a quote produced a selector that actually resolves.
    assert p.locator(by_label["it's a test"]["selector"]).count() == 1
    p.close()


# --- visual inspection (report.py) ---------------------------------------

def test_measure_locates_every_node_on_the_real_page(page):
    from zerodom import report

    graph = ZeroDOM.from_page(page)
    layout = report.measure(page, graph)

    # Every node the parser found must be findable and have real geometry.
    assert set(layout["boxes"]) == {n["id"] for n in graph["nodes"]}
    assert all(b["w"] > 0 and b["h"] > 0 for b in layout["boxes"].values())
    assert layout["width"] > 0 and layout["height"] > 0


def test_annotate_draws_one_badge_per_node_and_cleans_up(page):
    from zerodom import report

    graph = ZeroDOM.from_page(page)
    layout = report.measure(page, graph)

    drawn = report.annotate(page, layout)
    assert drawn == len(layout["boxes"])
    assert page.locator(f"#{report.LAYER_ID}").count() == 1

    report.clear(page)
    assert page.locator(f"#{report.LAYER_ID}").count() == 0
    # The badge layer must not survive into a later parse.
    assert "zd-badge" not in ZeroDOM.from_page(page).to_json()


def test_screenshot_writes_an_annotated_png(page, tmp_path):
    from zerodom import report

    out = tmp_path / "shot.png"
    graph = ZeroDOM.from_page(page)
    png, layout = report.screenshot(page, graph, str(out))

    assert out.exists() and out.stat().st_size > 0
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # The annotated file is larger than the clean capture: badges are on it.
    assert out.stat().st_size > len(png)
    assert len(layout["boxes"]) == len(graph["nodes"])


def test_html_report_is_self_contained_and_linked(page, tmp_path):
    from zerodom import report

    graph = ZeroDOM.from_page(page)
    png, layout = report.screenshot(page, graph)
    html = graph.to_html_report(png, layout)

    out = tmp_path / "report.html"
    out.write_text(html, encoding="utf-8")
    assert out.exists()

    # Self-contained: the image is inlined, nothing is fetched over the network.
    assert "data:image/png;base64," in html
    assert "src=\"http" not in html and "@import" not in html
    # One graph row and one overlay box per node, cross-referenced by data-id.
    for node in graph["nodes"]:
        assert f'class="row" data-id="{node["id"]}"' in html
        assert f'class="box" data-id="{node["id"]}"' in html
    assert html.count('class="box"') == len(graph["nodes"])
    # Hover wiring is present.
    assert "mouseenter" in html and "classList.add('on')" in html


def test_html_report_renders_without_a_screenshot():
    from zerodom import parse_html

    html = parse_html("<title>T</title><button id='b'>Go</button>").to_html_report()
    assert "No screenshot captured" in html
    assert 'data-id="node_01"' in html


def test_numeric_ids_resolve_in_the_real_browser(browser):
    """Regression: `#49151933` throws in querySelector; `[id="49151933"]` resolves."""
    from zerodom import report

    p = browser.new_page()
    p.set_content(
        "<html><title>T</title><body><div id='49151933'>"
        "<a href='/story'>Story</a></div></body></html>"
    )
    graph = ZeroDOM.from_page(p)
    assert graph["nodes"][0]["selector"].startswith('[id="49151933"]')
    assert p.locator(graph["nodes"][0]["selector"]).count() == 1
    # And it is measurable, which is what the overlay needs.
    assert set(report.measure(p, graph)["boxes"]) == {n["id"] for n in graph["nodes"]}
    p.close()


def test_shadow_dom_nodes_are_found_and_click_the_right_element(browser):
    """Regression: shadow content was invisible, and light selectors matched into it.

    `page.content()` omits shadow roots, so `#host > button` looked unique in the HTML
    while Playwright — which pierces open roots — matched two elements and clicked
    whichever came first.
    """
    html = """<html><body>
      <div id="host"><button onclick="mark('light')">Light</button></div>
      <div id="card"></div>
      <script>
        window.hits = []; function mark(v) { window.hits.push(v) }
        // <slot> renders the light child; without one the browser hides it entirely.
        host.attachShadow({mode: 'open'}).innerHTML =
          '<slot></slot><button onclick="window.hits.push(\\'shadow\\')">Shadow</button>';
        card.attachShadow({mode: 'open'}).innerHTML = '<input placeholder="Field">';
      </script>
    </body></html>"""
    p = browser.new_page()
    p.set_content(html)
    nodes = ZeroDOM.from_page(p)["nodes"]
    by_label = {n["label"]: n for n in nodes}

    assert {"Light", "Shadow", "Field"} <= set(by_label)
    assert all(p.locator(n["selector"]).count() == 1 for n in nodes), [
        (n["label"], n["selector"], p.locator(n["selector"]).count()) for n in nodes
    ]

    # Clicking must reach the tree the label came from, not merely resolve.
    p.click(by_label["Shadow"]["selector"])
    p.click(by_label["Light"]["selector"])
    assert p.evaluate("window.hits") == ["shadow", "light"]
    p.close()


def test_report_measures_shadow_nodes(browser):
    """A node Playwright can click must not be reported as unlocated."""
    from zerodom import report

    p = browser.new_page()
    p.set_content(
        "<html><body><div id='card'></div><script>"
        "card.attachShadow({mode:'open'}).innerHTML="
        "'<button style=\"width:80px;height:30px\">Shadow</button>';"
        "</script></body></html>"
    )
    graph = ZeroDOM.from_page(p)
    layout = report.measure(p, graph)
    assert len(layout["boxes"]) == len(graph["nodes"]) == 1
    p.close()


def test_stylesheet_hidden_controls_are_dropped(browser):
    """The cascade is invisible to a parser reading HTML text.

    `<input class="anomaly">` looks perfectly clickable in the source; only the
    browser knows a stylesheet hid it. Emitting it costs the agent a 30s
    Playwright timeout on an element it can never click.
    """
    p = browser.new_page()
    p.set_content(
        "<html><head><style>.gone{display:none}.invis{visibility:hidden}</style>"
        "</head><body>"
        "<button>Visible</button>"
        "<input class='gone' name='ghost1'>"
        "<input class='invis' name='ghost2'>"
        "</body></html>"
    )
    labels = [n["label"] for n in ZeroDOM.from_page(p)["nodes"]]
    assert labels == ["Visible"]
    # The page belongs to the caller: no marker attributes may survive the parse.
    assert p.evaluate("document.querySelectorAll('[data-zerodom-hidden]').length") == 0
    p.close()


def test_icon_link_label_survives_a_real_browser(browser):
    """The Hacker News vote arrow, as the browser actually builds it."""
    p = browser.new_page()
    p.set_content(
        "<a id='up_1' href='vote?id=1'><div class='votearrow' title='upvote'></div></a>"
    )
    nodes = ZeroDOM.from_page(p)["nodes"]
    assert [n["label"] for n in nodes] == ["upvote"]
    assert p.locator(nodes[0]["selector"]).count() == 1
    p.close()


def test_offscreen_content_is_not_treated_as_hidden(browser):
    """`content-visibility: auto` skips *rendering* offscreen content; it is still
    real, scrollable and clickable. Counting it as hidden deleted 129 of 177
    controls on vercel.com in 0.0.2 — worse than the bug it was fixing."""
    p = browser.new_page()
    p.set_content(
        "<html><head><style>.lazy{content-visibility:auto}</style></head><body>"
        "<div style='height:200vh'></div>"
        "<section class='lazy'><button>Below the fold</button></section>"
        "</body></html>"
    )
    assert [n["label"] for n in ZeroDOM.from_page(p)["nodes"]] == ["Below the fold"]
    p.close()


def test_display_contents_wrapper_is_not_treated_as_hidden(browser):
    """`display: contents` makes the element generate no box of its own —
    checkVisibility() correctly says "no" for it — but its children render
    completely normally, real boxes and all. Confirmed live on reddit.com:
    the entire post feed sits inside one such wrapper (an i18n passthrough
    div), and marking it hidden deleted all 27 real, visible posts
    underneath in one shot, since a hidden subtree is pruned outright."""
    p = browser.new_page()
    p.set_content(
        "<html><body>"
        "<div style='display:contents'><button>Real post title</button></div>"
        "</body></html>"
    )
    assert [n["label"] for n in ZeroDOM.from_page(p)["nodes"]] == ["Real post title"]
    p.close()


FRAME_HOST = """<html><body>
  <button>Top level</button>
  <iframe width="400" height="200"
          srcdoc="<button>Inside srcdoc</button><a href='/x'>Srcdoc link</a>"></iframe>
  <iframe width="10" height="10" srcdoc="<button>Tracking pixel</button>"></iframe>
  <iframe width="400" height="200" style="display:none"
          srcdoc="<button>Hidden frame</button>"></iframe>
</body></html>"""


def test_frames_are_ignored_unless_asked_for(browser):
    p = browser.new_page()
    p.set_content(FRAME_HOST)
    labels = [n["label"] for n in ZeroDOM.from_page(p)["nodes"]]
    assert labels == ["Top level"]
    p.close()


def test_frames_true_reaches_inside_and_the_nodes_are_clickable(browser):
    """A node inside a frame is worthless unless it can also be acted on."""
    from zerodom.frames import locate

    p = browser.new_page()
    p.set_content(FRAME_HOST)
    graph = ZeroDOM.from_page(p, frames=True)
    labels = [n["label"] for n in graph["nodes"]]
    assert labels == ["Top level", "Inside srcdoc", "Srcdoc link"]
    # srcdoc keeps the about:blank URL; filtering on URL would have missed it.
    assert graph["metadata"]["frames_read"] == 1
    for node in graph["nodes"]:
        assert locate(p, node).count() == 1, node
    p.close()


def test_pixel_and_hidden_frames_are_skipped(browser):
    """Ad pixels and display:none frames outnumber real ones on a commercial page."""
    p = browser.new_page()
    p.set_content(FRAME_HOST)
    labels = [n["label"] for n in ZeroDOM.from_page(p, frames=True)["nodes"]]
    assert "Tracking pixel" not in labels and "Hidden frame" not in labels
    p.close()


def test_frame_nodes_are_renumbered_densely(browser):
    p = browser.new_page()
    p.set_content(FRAME_HOST)
    ids = [n["id"] for n in ZeroDOM.from_page(p, frames=True)["nodes"]]
    assert ids == ["node_01", "node_02", "node_03"]
    p.close()


def test_iframe_warning_is_dropped_once_frames_are_read(browser):
    """"content is probably in an iframe" is wrong advice after we read them."""
    p = browser.new_page()
    p.set_content(
        "<html><body><!--" + "p" * 5000 + "-->"
        "<iframe width='400' height='200' srcdoc=\"<button>Inner</button>\"></iframe>"
        "</body></html>"
    )
    assert "warning" in ZeroDOM.from_page(p)["metadata"]
    assert "warning" not in ZeroDOM.from_page(p, frames=True)["metadata"]
    p.close()


def test_script_injected_head_content_does_not_shift_body_indexes(browser):
    """A <div> appended into <head> used to relocate the whole head into <body>.

    Re-parsing HTML text treats flow content in head as the implicit start of
    body, so every `body > div:nth-of-type(N)` path shifted. On europa.eu that
    silently broke 12 of 47 selectors — they resolved to nothing.
    """
    p = browser.new_page()
    p.set_content(
        "<html><head><title>T</title></head><body>"
        "<div>one</div><div><button>Target</button></div>"
        "</body></html>"
    )
    p.evaluate("() => document.head.appendChild(document.createElement('div'))")
    graph = ZeroDOM.from_page(p)
    node = next(n for n in graph["nodes"] if n["label"] == "Target")
    assert p.locator(node["selector"]).count() == 1
    assert graph["metadata"]["page_title"] == "T"
    p.close()


def test_async_page_reads_nested_frames_concurrently():
    """The async path is a separate implementation and shipped a one-level
    shortcut once already. Drive it for real: a frame inside a frame, read
    through asyncio.gather, with every node still clickable."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from playwright.async_api import async_playwright

    from zerodom.frames import locate

    inner = "<button>Deep</button>"
    middle = f'<button>Middle</button><iframe width=300 height=150 srcdoc="{inner}"></iframe>'
    outer = (
        "<html><body><button>Top</button>"
        f"<iframe width=500 height=300 srcdoc='{middle}'></iframe></body></html>"
    )

    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            await page.set_content(outer)
            await page.wait_for_timeout(300)
            graph = await ZeroDOM.from_page(page, frames=True)
            counts = [await locate(page, n).count() for n in graph["nodes"]]
            out = (
                [n["label"] for n in graph["nodes"]],
                counts,
                [len(n.get("frame") or ()) for n in graph["nodes"]],
            )
            await browser.close()
            return out

    # On its own thread: the sync `browser` fixture has already installed
    # Playwright's greenlet loop in the main one, and asyncio.run cannot share it.
    with ThreadPoolExecutor(1) as pool:
        labels, counts, depths = pool.submit(lambda: asyncio.run(run())).result()

    assert labels == ["Top", "Middle", "Deep"]
    assert counts == [1, 1, 1]
    # "Deep" lives two frames down — the bug this test exists for.
    assert depths == [0, 1, 2]
