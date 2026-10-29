# Coverage: does agent-browser go blind where ZeroDOM doesn't? — Mixed, no clean win

The hope was a knockout: "agent-browser misses interactive elements on real apps, ZeroDOM
catches them." Ran both tools on 10 app-style sites, same settle. **The honest result is
mixed — there is no reliable coverage advantage in either direction.**

Reproduce: `uv run python benchmarks/coverage_gap.py`.

## Actionable elements found (stable across repeats)

| site | agent-browser | ZeroDOM (`agent_snapshot`, full page) |
|---|---|---|
| nextjs.org | **90** | 73 |
| stripe.com | 201 | 192 |
| svelte.dev | 129 | 127 |
| supabase.com | 120 | 119 |
| linear.app | 132 | 133 |
| tailwindcss.com | 81 | 88 |
| vuejs.org | 57 | 67 |
| excalidraw.com | 28 | 29 |
| react.dev | 70 | **105** |
| vercel.com | 15 | **102** |

## Why this is NOT a clean win (the honest reading)

- **Most sites are a wash** — the two tools land within a handful of each other.
- **agent-browser sometimes finds more** (nextjs.org 90 vs 73). So "agent-browser under-renders"
  is false as a general claim.
- **The two big gaps (vercel, react) are mostly a scope-definition mismatch, not blindness.**
  ZeroDOM's `agent_snapshot` serializes the **full-page** DOM; agent-browser's tree returns a
  smaller slice on those pages. ZeroDOM's *own* default (`from_page`, viewport-only) returns
  just ~12 on vercel.com — so the 102 is "the whole long page" vs agent-browser's "top of the
  page", not "we see what they can't". Verified stable across runs and settle times (3/8/12s),
  so it's not a hydration artifact — but it's a difference in default scope, which an
  integrator could change on either side.

## Takeaway for the "will Firecrawl adopt" question

There is **no single technical knockout** here. Coverage is roughly even; the one axis that
holds up under scrutiny remains **cost** — ~48% fewer tokens at matching coverage and equal
state accuracy (`RESULTS_AGENT_SNAPSHOT.md`, `RESULTS_HEAD_TO_HEAD.md`). That is a real but
incremental advantage, not a "switch now" argument. `# ponytail:` a full-page-vs-viewport
scoped comparison (force both tools to the same scope) would sharpen this, but the honest
headline won't change: equally accurate, cheaper, not categorically better at seeing pages.
