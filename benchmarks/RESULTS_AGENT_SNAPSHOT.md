# ZeroDOM `agent_snapshot` vs `agent-browser snapshot -i`

Tokens: tiktoken `cl100k_base`. Baseline: the real Vercel Labs `agent-browser` CLI (`npm i -g agent-browser`). Reproducible with `benchmarks/bench_agent_snapshot.py`.

## Token cost at matching coverage

| Page | agent-browser (tok) | ZeroDOM (tok) | saved | actionable (ab→zd) | refs 1:1 |
|---|---:|---:|---:|---:|:--:|
| news.ycombinator.com | 5,096 | 2,669 | 48% | 231→231 | 100% |
| github.com/microsoft/playwright/issues | 4,803 | 2,174 | 55% | 146→147 | 100% |
| developer.mozilla.org/en-US/docs/Web/HTM | 2,210 | 1,151 | 48% | 145→145 | 100% |
| stripe.com | 4,557 | 1,780 | 61% | 196→197 | 100% |
| tailwindcss.com | 1,489 | 801 | 46% | 81→88 | 100% |
| nextjs.org | 1,604 | 817 | 49% | 73→73 | 100% |
| gitlab.com/explore | 3,985 | 2,973 | 25% | 155→177 | 100% |
| portswigger.net/web-security | 2,352 | 1,064 | 55% | 127→115 | 100% |
| **median / aggregate** | 26,096 | 13,429 | **48% / 49%** | | |

## Coverage divergence (reported separately, not a token comparison)

On these, the two tools captured materially different amounts of the page — agent-browser under-rendered the SPA where ZeroDOM's settle + full serialize saw more. A token ratio here would be meaningless.

| Page | agent-browser actionable | ZeroDOM nodes | ZeroDOM saw |
|---|---:|---:|---:|
| react.dev | 50 | 105 | 2.1× more |
| ant.design/components/button | 258 | 452 | 1.8× more |
| vercel.com | 11 | 102 | 9.3× more |
