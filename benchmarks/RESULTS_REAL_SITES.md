# `agent_snapshot` on 41 real websites — robustness + state on live DOMs

The synthetic tests prove the format; this proves the parser survives the real web.
`test_real_sites.py` loads each site in `sites.txt` **once**, settles, and takes a single
**read-only** `agent_snapshot` — no clicking, filling, submitting or crawling. Sequential,
1s apart, image/media/font aborted, normal UA, 45s timeout: it does exactly what a browser
does when you open the page, nothing more.

Run:  `uv run python benchmarks/test_real_sites.py`

## Result

| | |
|---|---|
| parsed without crashing | **41 / 41** |
| real content pages with live state markers | 36 / 37 |
| total state markers observed | 385 |
| nodes (median / max) | 88 / 491 |
| tokens (median / max) | 803 / 4817 |
| snapshot latency (median) | 2.1 s |

Zero crashes across messy, framework-built, real-world DOMs (React, Next, Vue, Svelte,
GitHub, Reddit, Stripe, news sites, gov sites). The state / `empty` markers fire on real
controls — e.g. Stripe 22, GitHub 30, Reddit 127, MDN 20, DuckDuckGo 20, and the httpbin
form's checkboxes/radios/empty inputs all captured.

## The 4 zero-node pages — classified (not hidden)

Four sites returned an empty snapshot. None is a parser failure; all four served an
anti-automation wall or error shell to a headless browser, i.e. there were genuinely no
interactive nodes on the page we were given:

| site | served page title | detection |
|---|---|---|
| stackoverflow.com/questions | "Just a moment…" | ✅ `blocked: {cloudflare, challenge-platform}` |
| ebay.com | "Error Page \| eBay" | block page (no `blocked` flag) |
| etsy.com | "etsy.com" | consent/bot interstitial |
| booking.com | "Untitled" | bot/consent interstitial |

An empty snapshot of an empty served page is the **correct** output, and respecting the
wall (not bypassing it) is the intended behaviour.

## Known limitation (named, not fixed)

`metadata.blocked` detects the Cloudflare challenge but not eBay's error page or a generic
empty/"Untitled" consent shell, so those show `blocked: None`. `# ponytail:` extending the
detector to title-based shells would be a fuzzy, false-positive-prone heuristic (an
"Untitled" page can be legitimate) — not worth it. The snapshot itself is already correct
(empty in, empty out); only the *reason* annotation is missing for the non-Cloudflare walls.
