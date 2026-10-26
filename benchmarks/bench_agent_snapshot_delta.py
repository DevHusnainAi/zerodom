"""Multi-turn cost: ZeroDOM delta snapshots vs re-snapshotting the full tree.

A single snapshot is only part of the story — an agent re-snapshots after EVERY
action, and that per-turn cost is what a long task actually pays. Firecrawl's
interactive agent (`scrape-interact/browser-agent.ts`) calls `agent-browser
snapshot -i` — the FULL tree — on every turn; it does not use agent-browser's own
`--delta`. ZeroDOM's `agent_snapshot_delta` sends the full tree once and then only
what changed, so this sums tokens across a K-turn run and compares the totals —
the honest "whole agent run" number against what the consumer actually does.

(agent-browser DOES have `--delta`; if a consumer adopted it the gap narrows to the
single-snapshot ~48% — see bench_agent_snapshot.py. This benchmark measures the
real-world pattern: full tree re-sent each turn.)

Each turn scrolls one viewport and re-snapshots (deterministic, no navigation, and
on a static page correctly a no-change delta — ~free for ZeroDOM, full-price for a
re-sent tree).

Setup:  npm i -g agent-browser && agent-browser install
Run:    uv run --directory <zerodom repo> python benchmarks/bench_agent_snapshot_delta.py
"""

from __future__ import annotations

import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

from zerodom.playwright_wrapper import ZeroDOM

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
    def tokens(s: str) -> int:
        return len(_ENC.encode(s))
except Exception:
    def tokens(s: str) -> int:
        return len(s) // 4

URLS = [
    "https://news.ycombinator.com",
    "https://github.com/microsoft/playwright/issues",
    "https://developer.mozilla.org/en-US/docs/Web/HTML",
    "https://stripe.com",
    "https://nextjs.org",
]
TURNS = 5  # the initial snapshot + (TURNS-1) scroll-and-re-snapshot turns
_SETTLE_S = 2.5


def _ab(args: list[str], timeout: int = 60) -> str:
    return subprocess.run(
        ["agent-browser", *args], capture_output=True, text=True, timeout=timeout
    ).stdout


def agent_browser_run(url: str) -> int:
    """Total tokens over the K-turn run, re-sending the FULL `snapshot -i` each turn
    — exactly what Firecrawl's browser-agent does (no --delta)."""
    _ab(["open", url])
    time.sleep(_SETTLE_S)
    total = tokens(_ab(["snapshot", "-i"]))
    for _ in range(TURNS - 1):
        _ab(["scroll", "down"])
        time.sleep(0.6)
        total += tokens(_ab(["snapshot", "-i"]))
    return total


def zerodom_run(page, url: str) -> int:
    """Total tokens ZeroDOM spends over the same run (full once, then deltas)."""
    page.goto(url, wait_until="load", timeout=45000)
    page.wait_for_timeout(1200)
    text, _refs, state = ZeroDOM.agent_snapshot_delta(page)
    total = tokens(text)
    for _ in range(TURNS - 1):
        page.evaluate("window.scrollBy(0, window.innerHeight)")
        page.wait_for_timeout(600)
        text, _refs, state = ZeroDOM.agent_snapshot_delta(page, since=state)
        total += tokens(text)
    return total


def main() -> None:
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for url in URLS:
            try:
                ab = agent_browser_run(url)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                zd = zerodom_run(page, url)
                page.close()
            except Exception as e:
                print(f"SKIP {url}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            if ab < 150:
                print(f"SKIP {url}: agent-browser run too small (likely blocked)", file=sys.stderr)
                continue
            rows.append((url, ab, zd))
            print(f"OK {url}: ab={ab} zd={zd} saved={100 * (ab - zd) / ab:.0f}%", file=sys.stderr)
        browser.close()

    if not rows:
        print("no usable samples", file=sys.stderr)
        sys.exit(1)

    import re
    import statistics as st
    print(f"# Multi-turn cost ({TURNS} turns): ZeroDOM delta vs full-tree-every-turn\n")
    print("Total tokens summed across the run. Baseline re-sends the full "
          "`agent-browser snapshot -i` each turn (Firecrawl's actual pattern); ZeroDOM "
          "sends the full tree once, then only deltas. Tokens: tiktoken `cl100k_base`.\n")
    print(f"| Page | full-every-turn total | ZeroDOM delta total | saved |")
    print("|---|---:|---:|---:|")
    for url, ab, zd in rows:
        print(f"| {re.sub(r'^https?://(www\\.)?', '', url)[:40]} | {ab:,} | {zd:,} | {100 * (ab - zd) / ab:.0f}% |")
    med = st.median(100 * (ab - zd) / ab for _, ab, zd in rows)
    tab, tzd = sum(r[1] for r in rows), sum(r[2] for r in rows)
    print(f"| **median / aggregate** | {tab:,} | {tzd:,} | **{med:.0f}% / {100 * (tab - tzd) / tab:.0f}%** |")


if __name__ == "__main__":
    main()
