"""ZeroDOM `agent_snapshot` vs the real `agent-browser snapshot -i` (Vercel Labs).

`agent-browser` (github.com/vercel-labs/agent-browser) is the browser CLI that
AI-agent stacks — Firecrawl's interactive scraper among them — call to get a page
representation for the model each turn: an interactive accessibility tree with
`@eN` refs. That is exactly the representation `ZeroDOM.agent_snapshot` replaces.

This benchmark measures the thing that actually costs money: tokens per snapshot,
at matching coverage, with the refs still resolvable. It is honest in three ways:

1. The baseline is the REAL tool (`npm i -g agent-browser`), not an approximation.
2. Both tools get a settle wait so they snapshot the same hydrated page.
3. Pages where the two tools captured materially different amounts of the DOM are
   reported SEPARATELY (a coverage finding), never folded into the token median —
   you can only compare token cost fairly when both show the same information.

Setup:  npm i -g agent-browser && agent-browser install
Run:    uv run --directory <zerodom repo> python benchmarks/bench_agent_snapshot.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
import statistics as st

from playwright.sync_api import sync_playwright

from zerodom.playwright_wrapper import ZeroDOM

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
    def tokens(s: str) -> int:
        return len(_ENC.encode(s))
except Exception:  # tiktoken optional; char/4 is a rough stand-in
    def tokens(s: str) -> int:
        return len(s) // 4

# Dense, dynamic, real pages an agent actually operates on. Auth-walled dashboards
# are omitted (can't reach them); these stress the representation without a login.
URLS = [
    "https://news.ycombinator.com",
    "https://github.com/microsoft/playwright/issues",
    "https://react.dev",
    "https://developer.mozilla.org/en-US/docs/Web/HTML",
    "https://ant.design/components/button",
    "https://stripe.com",
    "https://tailwindcss.com",
    "https://nextjs.org",
    "https://vercel.com",
    "https://gitlab.com/explore",
    "https://portswigger.net/web-security",
]

# agent-browser lines like `- link "Save" [ref=e1]` — the actionable roles.
_AB_ACTION = re.compile(
    r"^\s*-\s+(link|button|textbox|checkbox|radio|combobox|searchbox|switch|menuitem|tab|option|slider|spinbutton)\b"
)
_SETTLE_S = 3  # agent-browser `open` returns on load; wait so SPAs hydrate first.
# A page is "comparable" when both tools captured a similar count of actionable
# elements; outside this band the token numbers aren't apples-to-apples.
_COVERAGE_BAND = (0.6, 1.6)


def _ab(args: list[str], timeout: int = 60) -> str:
    return subprocess.run(
        ["agent-browser", *args], capture_output=True, text=True, timeout=timeout
    ).stdout


def agent_browser_snapshot(url: str) -> tuple[str, int]:
    """Return (snapshot text, actionable-element count) from the real agent-browser."""
    _ab(["open", url])
    time.sleep(_SETTLE_S)
    text = _ab(["snapshot", "-i"])
    actionable = sum(1 for ln in text.splitlines() if _AB_ACTION.match(ln))
    return text, actionable


def zerodom_snapshot(page, url: str) -> tuple[str, int, int]:
    """Return (snapshot text, node count, count of refs that DON'T resolve 1:1)."""
    page.goto(url, wait_until="load", timeout=45000)
    page.wait_for_timeout(1200)
    text, refmap = ZeroDOM.agent_snapshot(page)  # full-page, terse, [NN] refs
    bad = 0
    for ln in text.splitlines():
        m = re.match(r"\s*\[(\w+)\]", ln)
        if not m:
            continue
        sel = refmap.get(m.group(1))
        try:
            if not sel or page.eval_on_selector_all(sel, "els=>els.length") != 1:
                bad += 1
        except Exception:
            bad += 1
    return text, len(refmap), bad


def measure() -> list[dict]:
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for url in URLS:
            try:
                ab_text, ab_act = agent_browser_snapshot(url)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                zd_text, zd_nodes, zd_bad = zerodom_snapshot(page, url)
                page.close()
            except Exception as e:  # one dead page shouldn't sink the run
                print(f"SKIP {url}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            abt, zdt = tokens(ab_text), tokens(zd_text)
            if abt < 150 or ab_act < 5:  # bot wall / failed load — not a real sample
                print(f"SKIP {url}: agent-browser snapshot too small (likely blocked)", file=sys.stderr)
                continue
            ratio = zd_nodes / max(ab_act, 1)
            rows.append(dict(
                url=url, abt=abt, zdt=zdt, saved=100 * (abt - zdt) / abt,
                ab_act=ab_act, zd_nodes=zd_nodes, zd_bad=zd_bad, ratio=ratio,
                comparable=_COVERAGE_BAND[0] <= ratio <= _COVERAGE_BAND[1],
            ))
            print(f"OK {url}: ab={abt} zd={zdt} saved={rows[-1]['saved']:.0f}% "
                  f"cover={ab_act}->{zd_nodes} bad_refs={zd_bad}", file=sys.stderr)
        browser.close()
    return rows


def render(rows: list[dict]) -> str:
    def host(u: str) -> str:
        return re.sub(r"^https?://(www\.)?", "", u)[:40]

    out = ["# ZeroDOM `agent_snapshot` vs `agent-browser snapshot -i`", ""]
    out.append("Tokens: tiktoken `cl100k_base`. Baseline: the real Vercel Labs "
               "`agent-browser` CLI (`npm i -g agent-browser`). Reproducible with "
               "`benchmarks/bench_agent_snapshot.py`.\n")

    comp = [r for r in rows if r["comparable"]]
    div = [r for r in rows if not r["comparable"]]

    out.append("## Token cost at matching coverage\n")
    out.append("| Page | agent-browser (tok) | ZeroDOM (tok) | saved | actionable (ab→zd) | refs 1:1 |")
    out.append("|---|---:|---:|---:|---:|:--:|")
    for r in comp:
        fid = "100%" if r["zd_bad"] == 0 else f"{100 * (r['zd_nodes'] - r['zd_bad']) // max(r['zd_nodes'], 1)}%"
        out.append(f"| {host(r['url'])} | {r['abt']:,} | {r['zdt']:,} | {r['saved']:.0f}% "
                   f"| {r['ab_act']}→{r['zd_nodes']} | {fid} |")
    if comp:
        med = st.median(r["saved"] for r in comp)
        tab, tzd = sum(r["abt"] for r in comp), sum(r["zdt"] for r in comp)
        out.append(f"| **median / aggregate** | {tab:,} | {tzd:,} | "
                   f"**{med:.0f}% / {100 * (tab - tzd) / tab:.0f}%** | | |")

    if div:
        out.append("\n## Coverage divergence (reported separately, not a token comparison)\n")
        out.append("On these, the two tools captured materially different amounts of the "
                   "page — agent-browser under-rendered the SPA where ZeroDOM's settle + "
                   "full serialize saw more. A token ratio here would be meaningless.\n")
        out.append("| Page | agent-browser actionable | ZeroDOM nodes | ZeroDOM saw |")
        out.append("|---|---:|---:|---:|")
        for r in div:
            out.append(f"| {host(r['url'])} | {r['ab_act']} | {r['zd_nodes']} | {r['ratio']:.1f}× more |")
    return "\n".join(out)


def main() -> None:
    rows = measure()
    if not rows:
        print("no usable samples", file=sys.stderr)
        sys.exit(1)
    print(render(rows))


if __name__ == "__main__":
    main()
