# ZeroDOM vs agent-browser — head to head (same page, known state)

Does ZeroDOM capture state *more accurately* than agent-browser (Vercel Labs' `snapshot -i`,
the representation Firecrawl-style agent stacks consume), or just more cheaply? One
controlled page, its live DOM mutated through **properties** so they diverge from the HTML
attributes, snapshotted by **both** tools, each output checked against the state we set.

Reproduce: `uv run python benchmarks/head_to_head.py` (needs `agent-browser install`).

## Both outputs, same page

```
agent-browser                                    ZeroDOM
- checkbox "Notify" [checked=true, ref=e1]       [01] input Notify x
- textbox "Company" [ref=e2]: Acme Inc           [02] input Company =Acme Inc
- textbox "Password" [ref=e3]: •••••••           [03] input Password filled
- textbox "Empty" [ref=e4]                       [04] input Empty empty
- combobox "Country" [expanded=false]: Canada    [05] select Country =Canada
  - option "USA" [ref=e9]                         [06] button Advanced closed
  - option "Canada" [selected, ref=e10]           [07] div Overview *
- button "Advanced" [expanded=false, ref=e6]     [08] div Reports
- tab "Overview" [selected, ref=e7]
- tab "Reports" [ref=e8]
```

## Scorecard

| dimension | agent-browser | ZeroDOM | winner |
|---|---|---|---|
| checkbox checked (via property, no attribute) | ✅ | ✅ | tie |
| text field value | ✅ | ✅ | tie |
| `<select>` current value | ✅ | ✅ | tie |
| disclosure collapsed | ✅ | ✅ | tie |
| active tab | ✅ | ✅ | tie |
| **state accuracy** | **5/5** | **5/5** | **tie** |
| **tokens (this page)** | 130 | **72** | **ZeroDOM, 45% fewer** |
| tokens (real-site median, `bench_agent_snapshot`) | — | — | **ZeroDOM, 48%** |
| password value | masked, but **7 bullets = leaks length** | `filled` (no length) | **ZeroDOM** |
| empty field | implicit (absence of a value) | explicit `empty` | **ZeroDOM** (legibility) |

## Honest verdict

- **Accuracy: a tie.** agent-browser is *not* less accurate — it correctly reads live
  checked/value/select/expanded/selected state, just like ZeroDOM. The earlier "100%
  accurate" result is ZeroDOM vs the live DOM; it is **not** evidence that ZeroDOM beats
  agent-browser on accuracy. It does not. Claiming otherwise would be dishonest.
- **Cost: ZeroDOM wins, and that's the real edge** — ~45% fewer tokens here, 48% median on
  the real-site benchmark at matching coverage, refs 1:1. For an agent that re-snapshots
  every turn, that is the number that compounds into dollars.
- **Two smaller, real edges:** ZeroDOM hides a password's length where agent-browser's
  bullet count leaks it; and ZeroDOM's explicit `empty` marker is what took the decision
  eval from 92% to 100% on small models (agent-browser's absence-based empty is the same
  weak signal that tripped them). Neither is an accuracy difference — both are correct —
  but both help a cost- or capability-constrained consumer.

Bottom line: **ZeroDOM is not more accurate than what Firecrawl uses — it's equally
accurate at materially lower token cost, with marginally better secret-hygiene and
small-model legibility.**
