"""Turn a web page into the list of things an agent can click, and count the
savings. `zerodom <url>` for the compact graph, --find to search it."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

import tiktoken

from .parser import ZeroDOMParser, compact_line, find_nodes
from .playwright_wrapper import serialize

USER_AGENT = "Mozilla/5.0 (compatible; zerodom)"


def count_tokens(text: str) -> int:
    return len(tiktoken.get_encoding("cl100k_base").encode(text))


def fetch(url: str, render: bool, stealth: bool = False) -> tuple[str, str]:
    """Return (html, final_url). `render` runs a real browser for JS-heavy pages;
    `stealth` spawns a throwaway-profile Chrome over a CDP pipe (no port, no
    Playwright driver, nothing left on disk) — see cdp_pipe.py."""
    if stealth:
        from .cdp_pipe import fetch as pipe_fetch

        return pipe_fetch(url)
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


def emit_jsonl(graph) -> None:
    """The flattened 1D node array as clean JSONL: one page header line, then one
    node per line. Greppable, streamable, zero pretty-printing overhead."""
    meta = graph["metadata"]
    out = sys.stdout
    out.write(json.dumps({"url": meta["url"], "nodes": len(graph["nodes"])}) + "\n")
    for node in graph["nodes"]:
        out.write(json.dumps(node, ensure_ascii=False) + "\n")
    out.flush()


def inspect(
    url: str,
    render: bool = False,
    as_json: bool = False,
    screenshot: str | None = None,
    html_path: str | None = None,
    find: str | None = None,
    frames: bool = False,
    stealth: bool = False,
    as_pipe: bool = False,
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
        # Keep the common call 2-arg so callers (and tests) that wrap `fetch`
        # aren't forced to know about stealth; widen only when it's requested.
        html, final_url = fetch(url, render, stealth) if stealth else fetch(url, render)
        graph = ZeroDOMParser(html, final_url).parse()

    if as_pipe:  # machine path: JSONL to stdout, no token report
        emit_jsonl(graph)
        return 0

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
    # --json is a machine payload; keep stdout pure JSON so `zerodom … --json | jq`
    # works, and send the human token report to stderr instead.
    report = sys.stderr if as_json else sys.stdout
    print(f"\n{'─' * 52}", file=report)
    print(f"  URL                {final_url}", file=report)
    print(f"  Interactive nodes  {graph['metadata']['total_interactive_nodes']}", file=report)
    print(f"  Parsing latency    {graph['metadata']['parsing_latency_ms']} ms", file=report)
    print(f"  Raw DOM tokens     {raw_tokens:,}", file=report)
    print(f"  ZeroDOM tokens     {graph_tokens:,}  ({'json' if as_json else 'compact text'})", file=report)
    print(f"  Token savings      {savings:.1f}%", file=report)
    if extra:
        print(extra, file=report)
    print("─" * 52, file=report)
    return 0


def _stdin_urls():
    """URLs piped on stdin, one per line; blank lines and `#` comments skipped."""
    for line in sys.stdin:
        line = line.strip()
        if line and not line.startswith("#"):
            yield line


def scan_targets(args) -> int:
    """Run the surface ruleset over each target, stream findings as JSONL.

    Exit is non-zero only under --fail-on-finding, so a plain scan stays
    pipe-friendly (`... | jq` sees a clean stream, shell sees success).
    """
    from .surfaces import evaluate, load_rules

    rules = load_rules(args.rules)  # loads once; raises loudly on a bad ruleset
    urls = _stdin_urls() if args.url == "-" else [args.url]
    any_finding = False
    for url in urls:
        target = url
        if "://" not in target and Path(target).exists():
            target = Path(target).resolve().as_uri()
        html, final_url = fetch(target, args.render, args.stealth) if args.stealth else fetch(target, args.render)
        graph = ZeroDOMParser(html, final_url).parse()
        for finding in evaluate(graph, rules):
            any_finding = True
            sys.stdout.write(json.dumps(finding.to_dict(), ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 1 if (any_finding and args.fail_on_finding) else 0


def extension_dir() -> Path:
    """The unpacked extension: bundled in the wheel, or the repo copy in a source checkout."""
    here = Path(__file__).resolve().parent
    for d in (here / "extension", here.parent / "extension"):
        if (d / "manifest.json").is_file():
            return d
    raise SystemExit("zerodom: extension files not found in this install")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zerodom", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    aud = sub.add_parser(
        "audit", help="check an existing test suite's selectors against a live page"
    )
    aud.add_argument("path", help="file or directory of tests to read selectors from")
    aud.add_argument("--url", required=True, help="the running app to resolve them against")
    aud.add_argument(
        "--fail-on-ambiguous",
        action="store_true",
        help="exit non-zero if any selector matches more than one element (for CI)",
    )

    sub.add_parser(
        "relay",
        help="bridge Claude to your real, logged-in Chrome via zerodom's own extension",
    )

    ext = sub.add_parser(
        "extension", help="print the bundled Chrome extension's directory, for Load unpacked"
    )
    ext.add_argument("action", nargs="?", choices=["path"], default="path")

    scan = sub.add_parser(
        "scan", help="match a page's interaction graph against client-side surface rules"
    )
    scan.add_argument("url", help="URL (or '-' to read URLs from stdin, one per line)")
    scan.add_argument("--rules", metavar="PATH", help="YAML ruleset (default: bundled surfaces.yaml)")
    scan.add_argument("--render", action="store_true", help="fetch via headless Chromium")
    scan.add_argument("--stealth", action="store_true", help="fetch via a throwaway-profile Chrome over a CDP pipe")
    scan.add_argument("--fail-on-finding", action="store_true", help="exit non-zero if any rule fires (for CI)")

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
        "--stealth",
        action="store_true",
        help="fetch via a throwaway-profile Chrome over a CDP pipe (no port, no Playwright)",
    )
    insp.add_argument(
        "--pipe",
        dest="as_pipe",
        action="store_true",
        help="emit the node array as JSONL to stdout (one node per line) instead of the report",
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

    # `inspect` is the common verb, so requiring it is ceremony: `zerodom <url>`
    # works, and the explicit form keeps working for anyone who learned it.
    argv = sys.argv[1:] if argv is None else argv
    if (
        argv
        and argv[0] not in {"inspect", "audit", "relay", "scan", "extension", "-h", "--help"}
        and not argv[0].startswith("-")
    ):
        argv = ["inspect", *argv]

    args = parser.parse_args(argv)
    if args.command == "audit":
        from .audit import run

        ambiguous, text = run(args.path, args.url)
        print(text)
        return 1 if (ambiguous and args.fail_on_ambiguous) else 0

    if args.command == "relay":
        from .relay import main as relay_main

        relay_main()
        return 0

    if args.command == "scan":
        return scan_targets(args)

    if args.command == "extension":
        path = extension_dir()
        # Path alone on stdout so `cd "$(zerodom extension)"` works; the how-to goes to stderr.
        print(path)
        print("chrome://extensions -> Developer mode -> Load unpacked -> select the directory above",
              file=sys.stderr)
        return 0

    # `-` means: read URLs from stdin, one per line (cat targets.txt | zerodom inspect -).
    if args.url == "-":
        rc = 0
        for url in _stdin_urls():
            rc |= inspect(
                url, args.render, args.as_json, args.screenshot,
                args.html_path, args.find, args.frames, args.stealth, args.as_pipe,
            )
        return rc

    return inspect(
        args.url, args.render, args.as_json, args.screenshot,
        args.html_path, args.find, args.frames, args.stealth, args.as_pipe,
    )


if __name__ == "__main__":
    sys.exit(main())
