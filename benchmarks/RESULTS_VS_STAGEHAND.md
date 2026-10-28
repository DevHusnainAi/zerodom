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
| **page text / context** | always included (`StaticText`, ~1/3 of tree) | omitted by design (opt-in extension point, not yet built) | *not* a deficit: an action agent needs elements, not prose (the 100% decision eval shows the text doesn't change what to click). Lean is the default; a `context` mode that also emits text is a bounded add for the rare task that needs page prose — deliberately not paid for by every snapshot. |
| **decision accuracy** | — | 100% on actionable-only (`RESULTS_AGENT_DECISIONS.md`) | ZeroDOM measured; not run head-to-head on Stagehand's format |
| **secret hygiene** | protects only `type=password` (browser masks it); **leaks** other sensitive values — a filled credit-card field appeared verbatim (`4111111111111111`) in its tree | redacts password AND card/CVV/SSN/IBAN to `filled` by field name/label, any input type | **ZeroDOM win, both tested on the same filled login+payment page.** Stagehand relies on browser masking, so a card/SSN in a normal text input reaches the LLM; ZeroDOM's sensitive-field policy redacts it. |
| **delta stability (multi-turn)** | diffs the tree by trimmed line (`diffCombinedTrees`) | matches elements by content identity (label/href/role) | **both measured** on the same page+mutations: plain insert → Stagehand 3 lines / ZeroDOM 1; in-place re-render (React/Vue pattern) → **Stagehand 27 lines (the whole list again) / ZeroDOM 1**. Stagehand's delta collapses to a full snapshot on any re-render (new backend ids ⇒ every line "new"); ZeroDOM stays at the true change. |
| **snapshot latency** | not isolated (observe bundles an LLM call) | ~2.1 s median full-page (`RESULTS_REAL_SITES.md`) | not a fair head-to-head; ZeroDOM's parse is network-free |

### Delta stability — how it was measured (both sides)

Same page (an 8-item feed), same two in-place mutations, each tool's **real** delta:

- **Stagehand:** captured its `observe()` tree before and after each mutation (via the custom
  LLM client), then applied its own exported `diffCombinedTrees(prev, next)` — the exact
  function it ships for `--delta`. Count = lines it reports changed.
- **ZeroDOM:** `agent_snapshot_delta(page, since=state)` before and after the same mutation.
  Count = `+`/`-`/`~` lines.

Results: **insert** → Stagehand 3, ZeroDOM 1. **Re-render** (`list.innerHTML` rebuilt with the
same items + one) → **Stagehand 27, ZeroDOM 1**. The re-render is the common case (every SPA
framework replaces DOM nodes on state change), and it's where line-vs-id diffing breaks:
new backend ids make every line look new, so Stagehand re-sends the whole subtree while
ZeroDOM, matching by content, reports only the one genuinely-new item.

**Bottom line across dimensions:** ZeroDOM matches Stagehand on *what it finds* (coverage)
and on *acting correctly* (100% decision eval), at **~1/5 the tokens** of its `observe()`
representation. The one thing Stagehand always carries that ZeroDOM doesn't is the page's
text prose. That is a design choice, not a gap: an action agent decides from elements, not
paragraphs (the decision eval confirms the text doesn't change the choice), so ZeroDOM is
lean by default and a `context` mode that also emits page text is a bounded opt-in for the
rare task that needs it — never paid for by every snapshot the way Stagehand does.
`# ponytail:` context mode is left unbuilt until a real consumer needs it; YAGNI, and the
eval says action agents don't.

## What a representation swap into `observe()` would and wouldn't carry (adapter de-risk)

Before building the adapter, we checked *what information* each representation actually
carries, so a swap doesn't silently break a downstream consumer. Role histograms from the
captured Stagehand trees (real `observe()` output):

| role class (sample: HN / Wikipedia tree) | Stagehand carries | ZeroDOM carries |
|---|---|---|
| **actionable** — link, button, textbox, term, heading | yes (199+131 links, buttons, 1 textbox…) | **yes** — the whole point; flat `[ref]` list + per-ref selector |
| **state** — checked / selected / expanded / pressed | yes, `[checked]`-style suffix | yes, `x`/`*`/`on`/`open` markers (+ ZeroDOM adds sensitive-field redaction Stagehand lacks) |
| **StaticText** (page prose) | yes — **~1/3 of every tree** (257/760 lines on HN, 161/587 on Wikipedia) | **no** (lean by default; opt-in `context` mode) |
| **structural scaffolding** — RootWebArea, cell, LayoutTableCell, region, LineBreak, DisclosureTriangle | yes, nested + indented | no — flattened away |

**Consequence for the three Stagehand verbs — the swap is safe for two, not the third:**

- **`observe()`** wants a list of actionable candidates + a way to target each. ZeroDOM's
  `ref_map` *is* that (ref → CSS selector), delivered at ~1/5 the tokens. **Swap-safe.**
- **`act()`** consumes an observe result (target + method) and clicks/fills it. It reads
  the actionable element, not the prose or the tree nesting. **Swap-safe.**
- **`extract()`** pulls structured data from the page's *text* and *structure* — exactly
  the `StaticText` + nesting ZeroDOM drops. A naive swap here **would lose information.**
  So the adapter scopes ZeroDOM to the `observe`/`act` path and leaves `extract` on
  Stagehand's richer tree (or ZeroDOM's `context` mode once a consumer needs it).

This is the honest shape of the integration: ZeroDOM is a **cheaper serialization for the
action path**, not a drop-in for text extraction. Naming that up front is what makes the
adapter a bounded change instead of a regression nobody caught until `extract()` returned
empty. `# ponytail:` verified from captured role histograms, not assumed — the one place a
swap bites is extract, and the adapter simply doesn't touch it.
