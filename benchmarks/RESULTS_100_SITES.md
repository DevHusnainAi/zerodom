# ZeroDOM vs Stagehand `observe()` — 100 real websites

The controlled tests proved the mechanism; this is the scale check. Both tools were run on
the same 100 real sites (`bench100.txt`) and compared on the two things measurable on any
live page: **token cost** and **actionable coverage**. Stagehand's trees came from its
**real `observe()` pipeline** (captured via a custom LLM client — see
`RESULTS_VS_STAGEHAND.md`); ZeroDOM from `agent_snapshot`.

> **This run supersedes the first 100-site pass.** The first pass (a) blocked images/fonts,
> which degraded some renders, and (b) folded ZeroDOM's *own* render failures into a
> "bot-walled" bucket, which quietly flattered the parity number. Both are fixed here: the
> resource block is gone, failures are counted honestly in three buckets, and the engine
> now has a **bounded network-idle gate** that fixed the sites that were under-rendering.
> See "What changed and why" at the end.

Reproduce: `bench_100_zerodom.py` (ZeroDOM pass) + the Stagehand capture harness, then
`bench_100_aggregate.py`.

## Result (100 sites)

Honest classification — ZeroDOM's render failures are **not** hidden as bot walls:

| bucket | count | meaning |
|---|---|---|
| **comparable** (both rendered) | 88 | fair token + coverage comparison |
| **ZeroDOM failed** (Stagehand rendered, ZeroDOM ~empty) | 4 | counts **against** ZeroDOM |
| **Stagehand failed** (ZeroDOM rendered, Stagehand ~empty) | 2 | counts against Stagehand |
| **both bot-walled** | 6 | genuine consent/anti-bot wall, fair to exclude |

### Tokens (88 comparable)
| | |
|---|---|
| median Stagehand `observe()` tree | 4,858 |
| median ZeroDOM snapshot | 812 |
| **median savings** | **81%** |
| mean savings | 80% |
| aggregate savings | 82% |
| ZeroDOM cheaper on | **87 / 88 sites** |

Savings distribution: `>80%`: 53 · `60–80%`: 33 · `40–60%`: 1 · `<0% (worse)`: 1.

### Coverage (actionable elements found, 88 comparable)
| | |
|---|---|
| median actionable — Stagehand | 90 |
| median actionable — ZeroDOM | 91 |
| within ±25% (parity) | **81 / 88 (92%)** |
| ZeroDOM found ≥25% more | 7 |
| **ZeroDOM found ≥25% fewer** | **0** |

**Where both tools render, ZeroDOM is never materially behind on coverage** (0 sites ≥25%
fewer, down from 3 in the first pass), at ~5× fewer tokens. The 7 "more" are mostly
full-page vs Stagehand returning a smaller slice.

## The honest cost: 4 sites ZeroDOM fails that Stagehand renders

No longer swept under "bot-walled":

| site | Stagehand | ZeroDOM | cause |
|---|---|---|---|
| techcrunch.com | 266 | 0 | **goto TimeoutError** (40s) — heavy page / consent redirect |
| w3.org | 48 | 2 | likely **anti-bot differential** — headless Chromium served a stub; Stagehand's fuller browser cleared it |
| congress.gov | 6 | 2 | near-bot-wall for both; Stagehand barely over threshold |
| economist.com | 6 | 0 | paywall/bot-wall; Stagehand barely over threshold |

Only techcrunch is a clean engine gap (a load-timeout strategy would help); the other three
are bot/anti-automation differences where Stagehand's browser fingerprint fares better. All
four are reported **against** ZeroDOM anyway — the benchmark no longer hides them.

## Full scorecard vs Stagehand (how each dimension was measured)

| dimension | result | measured on |
|---|---|---|
| **tokens** | ZeroDOM **~1/5** (81% median, 82% aggregate) | 100 real sites |
| **actionable coverage** | **tie / slight edge** (92% within ±25%, 0 materially behind) | 100 real sites |
| **decision accuracy** | **parity** — ZeroDOM 19/21 (90%) vs Stagehand 18/21 (86%); tie at this n | measured head-to-head, 3 models, both formats (`eval_head_to_head.py`) |
| **state-capture accuracy** | ZeroDOM 100%, zero misses | controlled + live DOM (`RESULTS_STATE_ACCURACY.md`) |
| **secret hygiene** | **ZeroDOM win** — Stagehand leaked a filled card number; ZeroDOM redacts card/CVV/SSN/IBAN + password | controlled login+payment page |
| **delta on re-render** | **ZeroDOM win** — 1 line vs Stagehand 27 (full subtree) | controlled insert/re-render |
| **render robustness** | **Stagehand slightly ahead** — 4 sites ZeroDOM fails vs 2 for Stagehand | 100 real sites (above) |
| **page text / context** | Stagehand includes prose; ZeroDOM lean by default, context an opt-in extension | design |

## Honest bottom line

Across 100 real sites, ZeroDOM delivers the **same actionable coverage as Stagehand's
`observe()` at ~1/5 the tokens** (81% median saving, cheaper on 87 of 88, and — after the
network-idle fix — **not materially behind on any comparable site**). Combined with the
controlled-test wins (equal decision accuracy, better secret hygiene, far less delta churn),
the case is **cheaper, safer, and steadier, at equal quality**.

The one place Stagehand is ahead is **raw render robustness on hostile pages**: its browser
fingerprint cleared 2 anti-bot/consent walls that plain headless Chromium did not, and it
tolerated one page ZeroDOM's `goto` timed out on. That's an integration detail (browser
stealth + load strategy), not a representation deficit — and it's now stated, not hidden.

## What changed and why (vs the first 100-site pass)

1. **Network-idle gate** (`settle_network_ms`, default 3000, tunable): the real failures
   weren't framework hydration — gitlab reports no SPA framework yet renders its project
   list from a **data fetch** landing ~1.5s after `load`. The old 200ms-quiet / 1.2s-cap
   settle resolved against the empty shell. A bounded `networkidle` wait fixes it:

   | site | first pass | this pass | Stagehand |
   |---|---|---|---|
   | gitlab.com/explore | 26 | **174** | 166 |
   | duckduckgo.com | 35 | **141** | 133 |
   | archive.org | 21 | **67** | 67 |
   | khanacademy.org | **0 (failed)** | **94** | 102 |

2. **Honest buckets**: ZeroDOM render failures counted against ZeroDOM, not excluded as
   bot walls.

3. **No resource blocking**: images/fonts load, so the network-idle gate settles on the
   real network and the DOM isn't starved of layout-driving assets.

`# ponytail:` 100 sites is a scale check, not a universe; coverage counting differs slightly
between a flat ref list and a nested a11y tree, so treat ±25% as the parity band. The
network-idle cap is tunable — a page that never idles reads as-of-cap, by design.
