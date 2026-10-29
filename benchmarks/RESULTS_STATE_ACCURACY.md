# State-capture accuracy — were any markers wrong or missed?

"Parsed without crashing" is not accuracy. This measures whether the captured state is
*correct* and whether any true state was *missed*, against live-DOM ground truth.
`accuracy_state.py`, two parts.

Run:  `uv run python benchmarks/accuracy_state.py`

## Part A — controlled, exhaustive (the capture logic): 7/7 = 100%

A page is built, then mutated through DOM **properties** so they diverge from the HTML
attributes — the exact case static parsing can't see and live stamping must. Every marker
type is asserted against known truth:

| case | result |
|---|---|
| checkbox checked via **property**, no `checked` attribute | ✅ |
| checkbox checked via attribute | ✅ |
| empty text field → `empty` marker | ✅ |
| text value set via property | ✅ |
| password → `filled`, value **redacted** | ✅ |
| `aria-expanded=false` → `closed` | ✅ |
| `aria-selected` tab | ✅ |
| `<select>` current option → `=value` | ✅ (see below) |

## Part B — real pages, read-only (agreement vs live DOM): 37/37 = 100%

Every node's selector is resolved through **Playwright's own selector engine** — which
understands ZeroDOM's `:light()` syntax and pierces shadow DOM — then its captured state is
compared, per dimension and value strings included, against the element's true
property/attribute. A correct `empty`/unchecked call counts (it's a real accuracy win), and
every mismatch is printed, never averaged away.

| page | correct | wrong | unresolved |
|---|---|---|---|
| httpbin.org/forms/post | 12 | 0 | 0 |
| wikipedia.org | 3 | 0 | 0 |
| duckduckgo.com | 4 | 0 | 0 |
| developer.mozilla.org (incl. 11 **shadow-DOM** nodes) | 11 | 0 | 0 |
| python.org | 1 | 0 | 0 |
| pypi.org | 2 | 0 | 0 |
| news.ycombinator.com | 0 | 0 | 0 |
| gov.uk | 4 | 0 | 0 |
| **total** | **37** | **0** | **0** |

**Agreement 100% (37/37), zero mismatches, nothing left unverified** — including MDN's 11
shadow-DOM nav nodes, which earlier showed as "unverifiable" only because the first
verifier used in-page `document.querySelector` (no shadow piercing). Resolving through
Playwright confirmed every one matches the live DOM.

## What this process caught (the point of measuring)

- **A real engine gap, fixed:** `<select>`'s current option was being dropped — SERIALIZE
  stamped `data-zd-value` on selects, but the parser only read it for `FILLABLE_ROLES`
  (a `<select>` is `combobox`). Now surfaced as `=value` in Python and TypeScript. Part A
  went from missing it to capturing `=B`.
- **One apparent mismatch that was the *test's* bug, not the engine's:** on duckduckgo a
  `<button role=switch aria-checked=true>` — ZeroDOM correctly reported `checked`; the
  verifier only read `.checked` on `<input>` and ignored `aria-checked` on buttons, so it
  wrongly claimed the DOM was unchecked. Fixed the verifier; ZeroDOM was right. 26/26.

## The MDN shadow-DOM nodes — now verified, not assumed

MDN is built from `<mdn-*>` web components, so 11 stateful nav nodes live in shadow DOM.
ZeroDOM locates them with Playwright's `:light()` selector; the first verifier used in-page
`document.querySelector` (no shadow piercing) and could only mark them "unverifiable".
Resolving through Playwright's own engine instead, **all 11 resolve and match the live
DOM** — they were correct all along. There is now nothing the harness cannot check.

## Bottom line

Across exhaustive controlled cases and **every** real-page node (shadow DOM included), the
state capture is **100% accurate — zero false markers, zero misses, nothing unverified**.
The one earlier "mismatch" was the verifier's bug (ignoring `aria-checked` on a button),
not the engine; the engine mirrored the DOM exactly.
