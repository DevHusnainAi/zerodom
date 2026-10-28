# ZeroDOM vs Stagehand `observe()` — 100 real websites

The controlled tests proved the mechanism; this is the scale check. Both tools were run on
the same 100 real sites (`bench100.txt`) and compared on the two things measurable on any
live page: **token cost** and **actionable coverage**. Stagehand's trees came from its
**real `observe()` pipeline** (captured via a custom LLM client — see
`RESULTS_VS_STAGEHAND.md`); ZeroDOM from `agent_snapshot`. Secret-hygiene and delta-churn
can't be measured on strangers' live pages (no password to fill, no re-render to force), so
those stay from the controlled tests and are recapped at the end.

Reproduce: `bench_100_zerodom.py` (ZeroDOM pass) + the Stagehand capture harness, then
`bench_100_aggregate.py`.

## Result (100 sites; 88 rendered for both, 12 bot-walled/empty on one side)

### Tokens
| | |
|---|---|
| median Stagehand `observe()` tree | 4,858 |
| median ZeroDOM snapshot | 812 |
| **median savings** | **81%** |
| mean savings | 81% |
| aggregate savings | 83% |
| ZeroDOM cheaper on | **87 / 88 sites** |

Savings distribution: `>80%`: 55 · `60–80%`: 30 · `40–60%`: 2 · `<0% (worse)`: 1.

### Coverage (actionable elements found)
| | |
|---|---|
| median actionable — Stagehand | 90 |
| median actionable — ZeroDOM | 88 |
| within ±25% (parity) | **78 / 88 (89%)** |
| ZeroDOM found ≥25% more | 7 |
| ZeroDOM found ≥25% fewer | 3 |

**Coverage is a wash at scale** — ZeroDOM finds the same actionable surface Stagehand does,
so the ~5× token reduction is representation efficiency, not seeing less.

## Full scorecard vs Stagehand (how each dimension was measured)

| dimension | result | measured on |
|---|---|---|
| **tokens** | ZeroDOM **~1/5** (81% median, 83% aggregate) | 100 real sites |
| **actionable coverage** | **tie** (89% within ±25%) | 100 real sites |
| **decision accuracy** | ZeroDOM 100% (24/24) | controlled, 3 models (`RESULTS_AGENT_DECISIONS.md`) |
| **state-capture accuracy** | ZeroDOM 100%, zero misses | controlled + live DOM (`RESULTS_STATE_ACCURACY.md`) |
| **secret hygiene** | **ZeroDOM win** — Stagehand leaked a filled card number; ZeroDOM redacts card/CVV/SSN/IBAN + password | controlled login+payment page |
| **delta on re-render** | **ZeroDOM win** — 1 line vs Stagehand 27 (full subtree) | controlled insert/re-render |
| **page text / context** | Stagehand includes prose; ZeroDOM lean by default, context an opt-in extension | design |

## Honest bottom line

Across 100 real sites, ZeroDOM delivers the **same actionable coverage as Stagehand's
`observe()` at ~1/5 the tokens** (81% median saving, cheaper on 87 of 88). Combined with the
controlled-test wins — equal decision accuracy, better secret hygiene, far less delta churn
on re-rendering pages — the case for ZeroDOM as a leaner serialization in an `observe()`-style
pipeline is no longer "cheaper at parity" but **cheaper, safer, and steadier, at equal
quality.** The one thing Stagehand always carries that ZeroDOM doesn't is page prose, which
an action agent doesn't need (the decision eval confirms it) and which ZeroDOM can emit
on demand if a context-heavy task ever calls for it.

`# ponytail:` 100 sites is a scale check, not a universe; coverage counting differs slightly
between a flat ref list and a nested a11y tree, so treat ±25% as the parity band, not
element-exact equality.
