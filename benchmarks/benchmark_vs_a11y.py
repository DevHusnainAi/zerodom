"""ZeroDOM vs Playwright's ARIA snapshot — the alternative everyone asks about.

    uv run python benchmarks/benchmark_vs_a11y.py [url ...]

`page.aria_snapshot(mode="ai")` is what Playwright MCP sends to a model, so it is
the honest baseline. Two things get measured:

  tokens     — the snapshot keeps headings, prose, images and generic containers;
               zerodom keeps only what an agent can act on.
  targetable — in default mode there are no ref handles, so acting on a node means
               `get_by_role(role, name=...)`, which is strict and fails when the
               (role, name) pair repeats. Counts how often it does.
"""

from __future__ import annotations

import re
import sys

from playwright.sync_api import sync_playwright

from benchmark_tokens import DEFAULT_URLS

from zerodom.cli import count_tokens
from zerodom.parser import ZeroDOMParser

# The ARIA roles a web agent can act on. The rest of the snapshot is structure
# and prose — the same bulk zerodom prunes.
ACTIONABLE = {
    "button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox",
    "listbox", "menuitem", "menuitemcheckbox", "menuitemradio", "option",
    "slider", "spinbutton", "switch", "tab",
}
# Snapshot lines look like: `- link "Hacker News":` / `- textbox "Search"`
LINE = re.compile(r'^\s*-\s+([a-z]+)(?:\s+"([^"]*)")?')


def roles(snapshot: str) -> list[tuple[str, str]]:
    hits = (LINE.match(line) for line in snapshot.splitlines())
    return [(m[1], m[2] or "") for m in hits if m and m[1] in ACTIONABLE]


def main(argv: list[str]) -> int:
    urls = [a for a in argv if not a.startswith("--")] or DEFAULT_URLS

    print(
        f"{'url':<40} {'aria':>8} {'aria/ai':>8} {'zerodom':>8} "
        f"{'aria act':>9} {'zd nodes':>9} {'targetable':>12}"
    )
    print("─" * 100)
    for url in urls:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(1500)  # let the app shell settle
                plain = page.aria_snapshot()
                for_ai = page.aria_snapshot(mode="ai")
                html = page.content()
            except Exception as exc:  # a bot wall shouldn't kill the whole run
                print(f"{url[:39]:<40} {type(exc).__name__}: {exc}")
                browser.close()
                continue
            browser.close()

        pairs = roles(plain)
        unique = sum(1 for pair in pairs if pairs.count(pair) == 1)
        graph = ZeroDOMParser(html, url).parse()
        print(
            f"{url[:39]:<40} {count_tokens(plain):>8,} {count_tokens(for_ai):>8,} "
            f"{count_tokens(graph.to_compact_text()):>8,} {len(pairs):>9} "
            f"{graph['metadata']['total_interactive_nodes']:>9} "
            f"{unique:>5}/{len(pairs):<6}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
