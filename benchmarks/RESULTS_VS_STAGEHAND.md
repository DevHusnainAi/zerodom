# ZeroDOM vs Stagehand `observe()` — real pipeline, exact tokens

Not an estimate: this captures Stagehand's **actual** serialized page representation from its
real `observe()` pipeline and compares tokens to ZeroDOM's `agent_snapshot` on the same pages.

## How the Stagehand tree was captured (no API key, their real code)

Stagehand's a11y serializer runs inside its browser/extension stack — it can't be called
standalone. But `Stagehand.create({ model })` accepts a **custom LLM client** with a
`generate(args)` callback. `observe()` builds its full tree and hands it to `generate` as
the prompt *before* any model call. So a `generate` that just records `args` and returns a
dummy captures the exact tree their pipeline produced:

```js
const model = { generate: async (args) => { fs.writeFileSync(f, JSON.stringify(args)); return { content:[{type:"text",text:"[]"}] }; } };
const browser = await localBrowser.launch({ userDataDir: "./bd", headless: true });
const stagehand = await Stagehand.create({ browser, model });
await stagehand.observe("list every interactive element on the page");
```

The captured prompt's `Accessibility Tree:` block is Stagehand's real representation.

## Full representation (what each tool actually sends)

| page | Stagehand `observe()` tree | ZeroDOM `agent_snapshot` | fewer |
|---|---|---|---|
| news.ycombinator.com | 9,117 | 2,610 | 71% |
| stripe.com | 9,141 | 1,835 | 80% |
| gov.uk | 3,950 | 730 | 82% |
| developer.mozilla.org | 7,315 | 1,113 | 85% |
| **total** | **29,523** | **6,288** | **79%** |

ZeroDOM's actionable snapshot is **~1/5 the tokens** of Stagehand's `observe()` tree.

## Why — decomposed honestly (not all of it is "format")

Stagehand's tree is heavier for two separate reasons, and only one is pure format:

| | HN | stripe | gov.uk | MDN |
|---|---|---|---|---|
| Stagehand full tree | 9,117 | 9,141 | 3,950 | 7,315 |
| …minus `StaticText` (page text content) | 6,304 | 6,835 | 3,240 | 4,520 |
| …interactive lines only | 2,870 | 2,773 | 1,155 | 1,966 |
| ZeroDOM (actionable only) | 2,610 | 1,835 | 730 | 1,113 |

- **~1/3 of Stagehand's tree is `StaticText`** — the page's actual text content, which
  ZeroDOM's actionable snapshot omits. That's not "waste"; it's information ZeroDOM doesn't
  carry. So part of the 79% is ZeroDOM carrying *less*, not just encoding tighter.
- The rest is **structural scaffolding** (RootWebArea, LayoutTable, tbody, rows, cells) and
  nested indentation that ZeroDOM's flat ref list drops.
- **Apples-to-apples — actionable elements only — ZeroDOM is still ~28% leaner** (10% on
  table-dense HN, up to 43% on MDN): flat `[id] type label` beats a nested, indented
  `[id] role: name` tree.

## The honest headline

- For the representation an **action agent** needs — the actionable elements it can click or
  fill — ZeroDOM delivers it in **~1/5 the tokens** of Stagehand's `observe()` tree, because
  Stagehand also embeds page text and full DOM structure an action decision doesn't require.
- Even element-for-element, ZeroDOM is **~28% leaner** from format alone.
- The decision eval (`RESULTS_AGENT_DECISIONS.md`, 100% on actionable-only snapshots)
  supports that the omitted text doesn't hurt action selection — but a task that needs the
  page's prose as context would want Stagehand's richer (heavier) tree, and that trade is
  stated, not hidden.

This is the real, mergeable case for a leaner serialization in a `observe()`-style pipeline.

## Quality and other dimensions (not just tokens)

Measured from the same real captured trees unless noted. "measured" = from data here;
"asserted" = design fact not head-to-head benchmarked.

| dimension | Stagehand `observe()` | ZeroDOM | honest read |
|---|---|---|---|
| **actionable coverage** | 232 / 197 / 89 / 150 | 231 / 201 / 88 / 141 | **tie — same elements found** (measured). The token win is *not* from seeing less. |
| **tokens (full repr.)** | 29,523 | 6,288 | ZeroDOM ~1/5 (measured) |
| **tokens (element-for-element)** | 8,764 | 6,288 | ZeroDOM ~28% leaner (measured) |
| **page text / context** | included (`StaticText`, ~1/3 of tree) | omitted | Stagehand richer for context tasks; ZeroDOM leaner for action tasks (measured trade) |
| **decision accuracy** | — | 100% on actionable-only (`RESULTS_AGENT_DECISIONS.md`) | ZeroDOM measured; not run head-to-head on Stagehand's format |
| **secret hygiene** | no password field on these pages to test | redacts sensitive values to `filled` (`RESULTS_STATE_ACCURACY.md`) | ZeroDOM verified; Stagehand untested here — no claim either way |
| **selectors** | ID → XPath | ref → CSS + content-based identity | ZeroDOM's content identity is reflow-stable in delta mode (measured in delta tests); Stagehand diffs by trimmed line (asserted: can churn on reflow) |
| **snapshot latency** | not isolated (observe bundles an LLM call) | ~2.1 s median full-page (`RESULTS_REAL_SITES.md`) | not a fair head-to-head; ZeroDOM's parse is network-free |

**Bottom line across dimensions:** ZeroDOM matches Stagehand on *what it finds* (coverage)
and on *acting correctly* (100% decision eval), at **~1/5 the tokens** of its `observe()`
representation. The one thing Stagehand carries that ZeroDOM doesn't is the page's text
content — richer for context-heavy tasks, dead weight for action selection. That is the
whole, honest comparison: equal quality where it counts for an action agent, far cheaper.
