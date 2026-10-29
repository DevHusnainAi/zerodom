"""Where does agent-browser go BLIND and ZeroDOM doesn't? (coverage, not cost)

Cost parity won't make anyone switch. "Your tool misses interactive elements on real apps
and ours catches them" might. This runs BOTH tools on client-rendered / app-style sites and
counts actionable elements each one found, with the same settle wait so neither is
sandbagged. It reports the gap in BOTH directions — a site where ZeroDOM finds FEWER is
reported just as loudly, or the result is worthless.

Reuses the fair extractors from bench_agent_snapshot.py (agent-browser gets `open` + a 3s
hydrate wait; ZeroDOM gets its own settle).

Needs:  agent-browser install
Run:    uv run python benchmarks/coverage_gap.py
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

import bench_agent_snapshot as bench

# Client-rendered / framework / app-style pages — where "load" fires long before the real
# interactive UI exists, so a tool that snapshots too early under-renders.
SITES = [
    "https://react.dev", "https://nextjs.org", "https://vuejs.org", "https://svelte.dev",
    "https://vercel.com", "https://stripe.com", "https://tailwindcss.com",
    "https://linear.app", "https://supabase.com", "https://excalidraw.com",
]
# Below this, a snapshot almost certainly hit a bot/consent wall, not a real under-render —
# comparing counts there is meaningless, so such a page is marked blocked and skipped.
_WALL = 3


def main() -> int:
    rows = []
    print(f"{len(SITES)} app-style sites — actionable elements found by each tool\n")
    print(f"  {'site':<34} {'agent-browser':>13} {'ZeroDOM':>9} {'gap':>7}")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        for url in SITES:
            try:
                ab_text, ab_act = bench.agent_browser_snapshot(url)
                pg = b.new_page()
                pg.goto(url, wait_until="load", timeout=45000)
                zd_text, zd_nodes, _ = bench.zerodom_snapshot(pg, url)
                pg.close()
            except Exception as e:
                print(f"  {url.split('//')[-1]:<34} skipped ({type(e).__name__})")
                continue
            if ab_act < _WALL and zd_nodes < _WALL:
                print(f"  {url.split('//')[-1]:<34} {'(both blocked)':>13}")
                continue
            gap = zd_nodes - ab_act
            rows.append((url, ab_act, zd_nodes, gap))
            flag = "  <-- agent-browser blind" if (ab_act < _WALL <= zd_nodes) or (zd_nodes >= 3 * max(ab_act, 1)) else ""
            flag = flag or ("  <-- ZeroDOM fewer" if ab_act >= 3 * max(zd_nodes, 1) and ab_act - zd_nodes > 10 else "")
            print(f"  {url.split('//')[-1]:<34} {ab_act:>13} {zd_nodes:>9} {gap:>+7}{flag}")
        b.close()

    if not rows:
        print("\nno comparable pages"); return 0
    print("\n" + "=" * 60)
    ab_blind = [r for r in rows if r[1] < _WALL <= r[2]]
    zd_under = [r for r in rows if r[1] - r[2] > 10 and r[1] >= 3 * max(r[2], 1)]
    big_zd = [r for r in rows if r[2] >= 3 * max(r[1], 1) and r[1] >= _WALL]
    print(f"agent-browser saw ~nothing where ZeroDOM saw a full page: {len(ab_blind)}/{len(rows)}")
    if ab_blind:
        print("  " + ", ".join(f"{r[0].split('//')[-1]} ({r[1]}->{r[2]})" for r in ab_blind))
    print(f"ZeroDOM caught >=3x more actionable elements: {len(big_zd)}/{len(rows)}")
    if big_zd:
        print("  " + ", ".join(f"{r[0].split('//')[-1]} ({r[1]} vs {r[2]})" for r in big_zd))
    print(f"ZeroDOM caught materially FEWER (honest, other direction): {len(zd_under)}/{len(rows)}")
    if zd_under:
        print("  " + ", ".join(f"{r[0].split('//')[-1]} ({r[1]} vs {r[2]})" for r in zd_under))
    tot_ab = sum(r[1] for r in rows); tot_zd = sum(r[2] for r in rows)
    print(f"\ntotal actionable: agent-browser {tot_ab}  ZeroDOM {tot_zd}  ({tot_zd-tot_ab:+d})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
