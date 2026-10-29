# Multi-turn cost (5 turns): ZeroDOM delta vs full-tree-every-turn

Total tokens summed across the run. Baseline re-sends the full `agent-browser snapshot -i` each turn (Firecrawl's actual pattern); ZeroDOM sends the full tree once, then only deltas. Tokens: tiktoken `cl100k_base`.

| Page | full-every-turn total | ZeroDOM delta total | saved |
|---|---:|---:|---:|
| news.ycombinator.com | 25,500 | 1,833 | 93% |
| github.com/microsoft/playwright/issues | 24,330 | 1,776 | 93% |
| developer.mozilla.org/en-US/docs/Web/HTM | 11,050 | 1,076 | 90% |
| stripe.com | 23,795 | 1,547 | 93% |
| nextjs.org | 8,020 | 752 | 91% |
| **median / aggregate** | 92,695 | 6,984 | **93% / 92%** |

These are low-change turns (scroll on mostly-static pages) — the common
read→act→read agent pattern, where a ZeroDOM delta is near-free. A page that
mutates heavily every turn would save less, but the baseline re-sends the full
tree regardless, so the full-snapshot cost dominates either way. Node identity is
content-based (href/placeholder/label), so a reflow is a single add rather than
churn; if churn ever outgrows a full listing the delta falls back to one, so it is
never worse than a snapshot. If a consumer instead adopted agent-browser's own
`--delta`, the gap narrows to the single-snapshot ~48% (see `bench_agent_snapshot.py`).
