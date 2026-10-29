"""Does `agent_snapshot` hold up on real, live websites? (robustness + state on real DOMs)

Loads each site in `sites.txt` ONCE, settles, and takes a single read-only
`agent_snapshot` — no clicking, filling, submitting or crawling. It checks three things
the synthetic tests can't: (1) the parser survives real, messy, framework-built DOMs
without crashing; (2) the state / `empty` markers actually appear on real forms and
controls; (3) token cost stays sane at real-page scale.

Good-citizen by construction: sequential (never concurrent), a delay between sites, one
page load each, image/media/font requests aborted so we pull minimal bandwidth, a normal
browser UA, and a hard per-site timeout. It only reads the rendered DOM — the same thing
any browser does when you open the page.

Run:  uv run python benchmarks/test_real_sites.py
"""

from __future__ import annotations

import pathlib
import statistics as st
import sys
import time

from playwright.sync_api import sync_playwright

from zerodom.playwright_wrapper import ZeroDOM

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
    def tokens(s: str) -> int: return len(_ENC.encode(s))
except Exception:
    def tokens(s: str) -> int: return len(s) // 4

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
DELAY_S = 1.0          # politeness gap between sites
PAGE_TIMEOUT_MS = 45000
# markers _state_suffix can emit — count how many real controls carry live state.
MARKERS = (" empty", " filled", " x", " *", " on", " current", " open", " closed", " =")


def _count_state(text: str) -> int:
    n = 0
    for line in text.splitlines():
        # strip the leading "[NN] type label" — markers are suffixes, so just tally hits
        n += sum(line.count(m) for m in MARKERS)
    return n


def load_sites(path: pathlib.Path) -> list[str]:
    return [ln.strip() for ln in path.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def main() -> int:
    sites = load_sites(pathlib.Path(__file__).parent / "sites.txt")
    rows = []
    print(f"{len(sites)} sites, read-only, sequential, {DELAY_S}s apart\n")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA)
        # abort heavy resources we don't need for the DOM — politer on the site's bandwidth
        def _route(r):
            (r.abort if r.request.resource_type in ("image", "media", "font") else r.continue_)()
        ctx.route("**/*", _route)
        for i, url in enumerate(sites, 1):
            page = ctx.new_page()
            t0 = time.perf_counter()
            try:
                page.goto(url, wait_until="load", timeout=PAGE_TIMEOUT_MS)
                text, refmap = ZeroDOM.agent_snapshot(page)
                ms = (time.perf_counter() - t0) * 1000
                nodes, toks, state = len(refmap), tokens(text), _count_state(text)
                rows.append((url, "ok", nodes, toks, state, ms))
                print(f"  [{i:>2}] OK   {url:<48} nodes={nodes:<4} tok={toks:<5} state={state:<3} {ms:>6.0f}ms")
            except Exception as e:  # one bad site must never sink the run
                rows.append((url, f"err:{type(e).__name__}", 0, 0, 0, 0))
                print(f"  [{i:>2}] ERR  {url:<48} {type(e).__name__}: {str(e).splitlines()[0][:60]}")
            finally:
                page.close()
                time.sleep(DELAY_S)
        browser.close()

    ok = [r for r in rows if r[1] == "ok"]
    err = [r for r in rows if r[1] != "ok"]
    print("\n" + "=" * 70)
    print(f"RESULT: {len(ok)}/{len(rows)} parsed cleanly, {len(err)} errors")
    if ok:
        print(f"  nodes  median {int(st.median(r[2] for r in ok))}  max {max(r[2] for r in ok)}")
        print(f"  tokens median {int(st.median(r[3] for r in ok))}  max {max(r[3] for r in ok)}")
        print(f"  sites with live state markers: {sum(1 for r in ok if r[4] > 0)}/{len(ok)}"
              f"  (total markers {sum(r[4] for r in ok)})")
        print(f"  snapshot latency median {int(st.median(r[5] for r in ok))}ms")
    if err:
        print("  errors: " + ", ".join(f"{r[0].split('//')[-1].split('/')[0]} ({r[1]})" for r in err))
    return 0


if __name__ == "__main__":
    sys.exit(main())
