"""Turn a web page into the list of things an agent can click, and count the
savings. `zerodom <url>` for the compact graph, --find to search it."""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

import tiktoken

from .parser import ZeroDOMParser, compact_line, find_nodes
from .playwright_wrapper import serialize

USER_AGENT = "Mozilla/5.0 (compatible; zerodom)"


def count_tokens(text: str) -> int:
    return len(tiktoken.get_encoding("cl100k_base").encode(text))


def fetch(url: str, render: bool) -> tuple[str, str]:
    """Return (html, final_url). `render` runs a real browser for JS-heavy pages."""
    if render:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(url, wait_until="networkidle")
            html, final_url = serialize(page), page.url
            browser.close()
            return html, final_url

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode(resp.headers.get_content_charset() or "utf-8", "replace"), resp.url


def fetch_with_frames(url: str):
    """Load in a real browser and read every readable iframe as well."""
    from playwright.sync_api import sync_playwright

    from .playwright_wrapper import ZeroDOM

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")
        graph, html, final_url = ZeroDOM.from_page(page, frames=True), serialize(page), page.url
        browser.close()
    return graph, html, final_url


def capture(
    url: str, screenshot: str | None, html_path: str | None, frames: bool = False
):
    """Load a URL once and write whichever visual artifacts were asked for.

    Measuring, annotating and screenshotting all need the same live page, so they
    share one browser session rather than one launch per artifact.
    """
    from playwright.sync_api import sync_playwright

    from . import report
    from .playwright_wrapper import ZeroDOM

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="networkidle")
        source, final_url = serialize(page), page.url
        graph = ZeroDOM.from_page(page, frames=frames)

        png, layout = report.screenshot(page, graph, screenshot)
        browser.close()

    located = len(layout["boxes"])
    notes = []
    if screenshot:
        notes.append(f"  Screenshot         {screenshot}  ({located}/{len(graph['nodes'])} located)")
    if html_path:
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(graph.to_html_report(png, layout))
        notes.append(f"  HTML report        {html_path}")
    return graph, source, final_url, "\n".join(notes)


def inspect(
    url: str,
    render: bool = False,
    as_json: bool = False,
    screenshot: str | None = None,
    html_path: str | None = None,
    find: str | None = None,
    frames: bool = False,
) -> int:
    # A local path is a perfectly good thing to inspect; both fetchers need file://.
    if "://" not in url and Path(url).exists():
        url = Path(url).resolve().as_uri()

    extra = ""
    if screenshot or html_path:
        # One browser session for the graph and every artifact asked for.
        graph, html, final_url, extra = capture(url, screenshot, html_path, frames)
    elif frames:
        # Frames only exist in a live browser, so this is a --render superset.
        graph, html, final_url = fetch_with_frames(url)
    else:
        html, final_url = fetch(url, render)
        graph = ZeroDOMParser(html, final_url).parse()

    raw_tokens = count_tokens(html)
    # Measure what a model would actually be sent: compact JSON, or the text DSL.
    payload = graph.to_json(indent=None) if as_json else graph.to_compact_text()
    graph_tokens = count_tokens(payload)
    savings = (1 - graph_tokens / raw_tokens) * 100 if raw_tokens else 0.0

    if find:
        hits = find_nodes(graph["nodes"], find)
        print("\n".join(compact_line(node) for node in hits) if hits
              else f"No node matches {find!r} among {len(graph['nodes'])} nodes.")
    else:
        print(graph.to_json() if as_json else graph.to_compact_text())
    print(f"\n{'─' * 52}")
    print(f"  URL                {final_url}")
    print(f"  Interactive nodes  {graph['metadata']['total_interactive_nodes']}")
    print(f"  Parsing latency    {graph['metadata']['parsing_latency_ms']} ms")
    print(f"  Raw DOM tokens     {raw_tokens:,}")
    print(f"  ZeroDOM tokens     {graph_tokens:,}  ({'json' if as_json else 'compact text'})")
    print(f"  Token savings      {savings:.1f}%")
    if extra:
        print(extra)
    print("─" * 52)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zerodom", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    insp = sub.add_parser("inspect", help="parse a URL and report token savings")
    insp.add_argument("url")
    insp.add_argument(
        "--render",
        action="store_true",
        help="fetch via headless Chromium instead of plain HTTP (for JS-rendered pages)",
    )
    insp.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit the full JSON graph instead of the token-dense text DSL",
    )
    insp.add_argument(
        "--frames",
        action="store_true",
        help="also read inside iframes (implies --render; embedded editors, payment fields)",
    )
    insp.add_argument(
        "--find",
        metavar="QUERY",
        help="print only the nodes whose type or label matches QUERY",
    )
    insp.add_argument(
        "--screenshot",
        metavar="OUT.png",
        help="write a full-page screenshot with a numbered badge over every node",
    )
    insp.add_argument(
        "--html",
        dest="html_path",
        metavar="OUT.html",
        help="write a self-contained HTML report: graph beside the annotated page",
    )

    # `inspect` is the only verb, so requiring it is pure ceremony: `zerodom <url>`
    # works, and the explicit form keeps working for anyone who learned it.
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] not in {"inspect", "-h", "--help"} and not argv[0].startswith("-"):
        argv = ["inspect", *argv]

    args = parser.parse_args(argv)
    return inspect(
        args.url, args.render, args.as_json, args.screenshot,
        args.html_path, args.find, args.frames,
    )


if __name__ == "__main__":
    sys.exit(main())
