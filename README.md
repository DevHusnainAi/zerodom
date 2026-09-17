<div align="center">

<img src="assets/banner.png" alt="ZeroDOM — the visual action layer for AI web agents. See the page. Know exactly what to click." width="100%">

<br/>

[![PyPI version](https://img.shields.io/pypi/v/zerodom)](https://pypi.org/project/zerodom/)
[![npm version](https://img.shields.io/npm/v/%40vexralabs%2Fzerodom)](https://www.npmjs.com/package/@vexralabs/zerodom)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://pypi.org/project/zerodom/)
[![TypeScript](https://img.shields.io/badge/typescript-5.6%2B-blue)](https://www.npmjs.com/package/@vexralabs/zerodom)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![CI](https://github.com/DevHusnainAi/zerodom/actions/workflows/ci.yml/badge.svg)](https://github.com/DevHusnainAi/zerodom/actions/workflows/ci.yml)
[![Claude MCP](https://img.shields.io/badge/Claude-MCP%20server-8A63D2.svg)](#10-second-mcp-setup)

</div>

<img src="assets/hero.png" alt="ZeroDOM report: numbered cyan badges [31], [45] overlaid on a live page alongside the compact interaction graph — the agent sees click [45], ZeroDOM resolves it to the exact DOM element">

---

> **An agent should see `click [45]`, and ZeroDOM resolves [45] to the exact DOM element** —
> no hallucinated CSS locators, no drifted pixel coordinates, no ambiguous `(role, name)` pairs
> that match the wrong element.

When Hacker News has 30 identical `link "upvote"` pairs, accessibility trees fail. ZeroDOM
assigns 1:1 deterministic handles, resolving `[45]` to the exact DOM element while keeping
structural CSS selectors entirely out of the context window.

<p align="center">
  <b><a href="https://zerodom.vexralabs.com">Site</a></b>
  &nbsp;·&nbsp;
  <b><a href="https://zerodom.vexralabs.com/docs">Docs</a></b>
  &nbsp;·&nbsp;
  <b><a href="https://zerodom.vexralabs.com/playground">Playground</a></b>
  &nbsp;·&nbsp;
  <b><a href="https://zerodom.vexralabs.com/compare">vs. ARIA snapshots</a></b>
  &nbsp;·&nbsp;
  <b><a href="#benchmarks">Benchmarks</a></b>
</p>

---

## Install

<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin: 1.5rem 0;">

**Python**

```bash
pip install zerodom
# or: uvx zerodom — the CLI runs straight off PyPI
```

**TypeScript / Node**

```bash
npm install @vexralabs/zerodom
```

</div>

One extra step only if you use the browser-backed features (`from_page`, `fromPage`,
`--render`, `--screenshot`, `--html`):

```bash
playwright install chromium
```

---

## Quickstart

**Python** — any Playwright page, sync or async:

```python
from zerodom import ZeroDOM

graph = ZeroDOM.from_page(page)      # any Playwright page, sync or async
print(graph.to_compact_text())       # what you send the model
selectors = graph.selector_map()     # {"node_01": "#email-input", ...} — stays your side
```

**TypeScript** — any object with `content()` / `url()`:

```ts
import { ZeroDOM } from "@vexralabs/zerodom";

const graph = await ZeroDOM.fromPage(page);   // any Playwright Page
console.log(graph.toCompactText());           // what you send the model
const selectors = graph.selectorMap();        // { node_01: "#email-input", ... } — stays your side
```

**Parse HTML you already have** (no browser needed):

```python
from zerodom import parse_html
graph = parse_html(html, url)
```

```ts
import { parseHtml } from "@vexralabs/zerodom";
const graph = parseHtml(html, url);
```

Real output from a Hacker News row, 438 bytes of HTML → 3 lines:

```text
PAGE: Hacker News | https://news.ycombinator.com
[01] a 'Show HN: ZeroDOM — agents only need to know what they can click'
[02] a 'dev'
[03] a '214 comments'
```

**11,882 tokens of Hacker News → 2,326.** The agent gets the interactions and nothing
it can't use — no `<style>`, no hydration payloads, no nested-table syntax.

---

## The problem is addressing, not size

An agent driving a browser gets one of two action spaces today, and both are bad.

**Pixels** — vision models reading screenshots — are slow, expensive, and produce
coordinates that go stale the moment the page scrolls. **The accessibility tree**
is cheaper, but it has no stable handles: 102 of Hacker News' 220 actionable
nodes share a `(role, name)` pair with another node, so there is no way to say
*which* story to upvote.

That second failure is the expensive one. A graph that costs a few tokens too
many wastes money. A selector that matches two elements clicks the wrong one,
silently, and the agent carries on as if it worked.

ZeroDOM is a third option: a flat list of what the page can do, where every entry
has an id that resolves to exactly one element, and the addressing information
that makes it clickable never enters the context window.

---

## 10-second MCP setup

```bash
playwright install chromium
```

**Claude Desktop** — `claude_desktop_config.json`
(`~/Library/Application Support/Claude/` on macOS,
`%APPDATA%\Claude\` on Windows):

```json
{
  "mcpServers": {
    "zerodom": {
      "command": "uvx",
      "args": ["--from", "zerodom", "zerodom-mcp"]
    }
  }
}
```

**Cursor** — `.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` globally:

```json
{
  "mcpServers": {
    "zerodom": {
      "command": "uvx",
      "args": ["--from", "zerodom", "zerodom-mcp"]
    }
  }
}
```

### Tools

| tool | what it does |
|---|---|
| `zerodom_parse_url(url, verbose=False)` | navigate, return the compact graph |
| `zerodom_read_page(verbose=False)` | re-read the live DOM **without navigating** |
| `zerodom_find(query)` | return only the nodes matching a phrase |
| `zerodom_click_node(node_id)` | click, then return **what changed** |
| `zerodom_fill_node(node_id, text)` | type, then return what changed |

**An agent loop shouldn't re-read the page it already has.** Two tools exist so it
doesn't have to. `zerodom_find` answers "where's the dispatch button?" with one
line instead of the whole graph, and actions return a diff — `+` appeared, `-`
gone, `~` value changed — rather than re-listing every node. On the bundled demo
page:

```text
zerodom_parse_url(...)     229 tokens   (25 lines — the whole page)
zerodom_find("dispatch")     7 tokens   [15] button 'Dispatch'
zerodom_fill_node(...)      14 tokens   no structural change
```

The saving compounds: it is the difference between an agent spending the full
graph on every one of twenty actions and spending it once. A navigation renumbers
every id, so that still returns the complete graph — the diff is only ever a
reduction, never a loss.

---

## Why ARIA snapshots fail

The fair comparison isn't raw HTML — nobody sends a model raw HTML. It's
Playwright's `page.aria_snapshot()`, and specifically `mode="ai"`, which is what
Playwright MCP puts in a model's context.

| page | ARIA | ARIA `mode="ai"` | **ZeroDOM** | **saved vs ai** | targetable by `(role, name)` |
|---|---:|---:|---:|---:|---:|
| airbnb.com | 1,677 | 3,346 | **1,692** | **49.4%** | 72/72 |
| github.com/…/issues | 8,287 | 12,375 | **2,976** | **76.0%** | 83/118 |
| en.wikipedia.org article | 7,585 | 12,958 | **3,060** | **76.4%** | 132/185 |
| news.ycombinator.com | 10,345 | 12,684 | **2,350** | **81.5%** | 118/220 |
| developer.mozilla.org | 4,068 | 5,934 | **1,677** | **71.7%** | 59/87 |

**Mean 71.0% fewer tokens than the snapshot a model actually gets.** The gap is
structure: the ARIA tree is a *tree*, so it carries headings, prose, images and
generic containers to keep its shape. ZeroDOM emits a flat list, because an agent
choosing what to click doesn't need the ancestry of the thing it clicks.

The last column is the sharper problem. Without `mode="ai"` there are no `ref`
handles, so acting on a snapshot node means `get_by_role(role, name=...)` — which
is strict and throws when the pair repeats. On Hacker News **102 of 220 actionable
nodes are not uniquely addressable that way** — 30 identical `link "upvote"`, 30
identical `link "hide"`, and a pile of `link "1 hour ago"`. Which story does the
model upvote? ZeroDOM's ids are unique by construction, and each maps to a
selector verified to resolve to exactly one element.

The honest unit is tokens per action:

| page | ZeroDOM | ARIA |
|---|---:|---:|
| airbnb.com | **9.9** | 23.3 |
| github.com/…/issues | **12.1** | 70.2 |
| en.wikipedia.org article | **11.6** | 41.0 |
| news.ycombinator.com | **10.2** | 47.0 |
| developer.mozilla.org | **9.9** | 46.8 |

**A median of ~10 tokens per action**, against ARIA's 23–70 and wildly
variable. Context cost scales with what a page can *do*, not with how it was
built — a budget you can plan around before you know which page the agent lands
on. There is no page in this set where ZeroDOM costs more per action.

---

## Benchmarks

### Measured on 111 live sites

`benchmarks/benchmark_sites.py` — static pages, SPAs, web components, iframes,
canvas apps, dashboards, commerce, government, forms and login walls:

| | |
|---|---|
| nodes audited | **10,756** |
| resolved to exactly one live element | **99.00%** |
| **ambiguous — matched more than one** | **0.03%** (3 nodes) |
| invalid selectors | **0** |
| actionable to Playwright (sampled) | 95.6% of 1,215 |
| unlabelled | 0.65% |
| tokens per node | median **10.2**, range 8.3–20.8 |
| saving vs raw HTML | median **98.9%**, worst **64.1%** |
| parse time | median **55ms**, p90 214ms |

**The hard cases are the point.** 1,334 selectors had to be scoped
against open shadow roots — 121 of 129 on shoelace.style, 85 of 95 on
vercel.com — and every one of them resolves uniquely. Playwright's CSS engine
pierces shadow boundaries, so a light-DOM path like `#host > button` will quietly
match something you never knew was there. That bug shipped in 0.0.1 and is why
this section leads.

### vs raw HTML

`uv run python benchmarks/benchmark_tokens.py` — `tiktoken`, `cl100k_base`:

| page | raw HTML | verbose JSON | **ZeroDOM compact** | **compact saved** |
|---|---:|---:|---:|---:|
| airbnb.com | 196,195 | 1,695 | **257** | **99.9%** |
| github.com/…/issues | 116,257 | 12,303 | **1,628** | **98.6%** |
| developer.mozilla.org | 29,472 | 16,098 | **1,677** | **94.3%** |
| en.wikipedia.org article | 37,535 | 17,011 | **2,961** | **92.1%** |
| news.ycombinator.com | 11,882 | 18,428 | **2,326** | **80.4%** |

**Mean 93.1% across these five.**

---

## `zerodom audit` — check the selectors you already have

Point this at a test suite you already have. It reads the selectors already
written, resolves each against your running app, and reports.

```bash
zerodom audit tests/ --url http://localhost:3000
```

```text
AMBIGUOUS  2 match more than one element — a click may hit the wrong one
  .btn  (3 matches)
      tests/checkout.spec.ts:41
  nav a  (2 matches)
      tests/nav.spec.ts:12

DEAD  1 match nothing on this page
  #gone
      tests/legacy.spec.ts:88

ambiguous 2 · dead 1 · invalid 1 · ok 214
```

---

## CLI

```bash
zerodom https://example.com                   # the compact graph + a token report
zerodom https://example.com --find "sign in"  # only the nodes that match
zerodom https://example.com --frames         # also read inside iframes
zerodom https://example.com --json            # the full graph, selectors included
zerodom https://example.com --render          # headless Chromium, for JS pages
```

---

## Seeing the graph

`[03] a 'new'` tells you node 3 exists. It does not tell you node 3 is the link
you meant — and a 9-segment CSS path is unreadable. So look at it:

```bash
zerodom https://news.ycombinator.com --screenshot page.png
zerodom https://news.ycombinator.com --html report.html
```

`--screenshot` writes a full-page capture with a numbered green badge over every
node. `--html` writes a self-contained report — graph on the left, page on the
right. Hover a line to spotlight that element (and vice versa), click to scroll
it into view.

<img src="assets/hero-hover.png" alt="hovering a row in the ZeroDOM report dims the page to one spotlighted element">

Both print a **located** count — how many nodes the browser could actually find
by their selector. `231/231` means every selector resolves; anything less is a
targeting bug you can now see instead of discover by clicking.

---

## How labels are resolved

In order, first hit wins: `<label for>` → wrapping `<label>` → `aria-labelledby`
→ `aria-label` / `placeholder` / `alt` / `title` → a submit input's `value` →
**adjacent caption text** (`Search: <input name="q">` → `Search`) → the element's
own text → `name` / `value` → an image-only control's `<img alt>`.

---

## Core differentiators

**No LLM in the loop.** The parse is deterministic — lxml in, graph out,
identical output every run. Nothing about your page reaches a model until *you*
send the graph to one.

**Selectors never enter the context window.** The model sees `[03]`; the CSS
path `#row > span > a` stays in `selector_map()` on your side. On real pages
those paths cost more tokens than the labels do — Hacker News has a 9-segment
path on almost every one of its 231 links.

**Nothing leaves your machine.** The browser is yours, the parse is local, the
graph is a dict you own. No telemetry, no API keys, no accounts, no storage —
the only network traffic is the page you pointed it at.

**Shadow DOM handled.** Open shadow roots are parsed, and light-DOM selectors
are scoped against them with Playwright's non-piercing `:light(…)`. Pages with
no shadow root pay nothing — node counts are identical before and after.

**Invalid CSS ids escaped.** Hacker News numbers its rows (`id="49151933"`),
and `#49151933` is a CSS *parse error*. Those ids become `[id="49151933"]`.

---

## Output schema

```json
{
  "nodes": [
    {"id": "node_01", "type": "input", "role": "textbox", "label": "Email Address",
     "selector": "#email-input", "placeholder": "user@example.com",
     "required": true, "value": "", "action": "fill"}
  ],
  "metadata": {"page_title": "Login", "url": "...",
               "total_interactive_nodes": 1, "parsing_latency_ms": 4.2}
}
```

---

## Limitations

Known and worth knowing before you build on it:

- **Closed shadow roots are unreachable.** Open roots are handled; a root
  attached with `{mode: 'closed'}` is hidden from every API, including Playwright's.

- **Iframes are opt-in.** Pass `frames=True` — `ZeroDOM.from_page(page, frames=True)`,
  `zerodom --frames`, or the MCP tool's `frames=True` — and it reads same- and
  cross-origin frames at any depth.

- **An almost-empty graph tells you why.** `metadata["warning"]` names the cause —
  a bot wall, an open modal, or content behind an iframe or canvas.

- **Canvas and WebGL apps have nothing to parse.** Figma-style surfaces draw their
  controls as pixels — there is no element to emit.

- **Nothing waits for the page to finish thinking.** `from_page` and
  `zerodom_read_page` snapshot the DOM at call time. Wait for your own condition
  first, then parse.

- **Anti-bot systems are out of scope, by design.** ZeroDOM is middleware over a
  `Page` you already control — it never fetches anything.

Full details in [SECURITY.md](SECURITY.md).

---

## Development

```bash
uv sync
uv run playwright install chromium   # needed for the browser-backed tests
uv run pytest                        # 131 tests; browser ones skip without chromium
uv build                             # wheel + sdist into dist/
```

**TypeScript port:**

```bash
cd js
npm install
npm run build
npm test                             # 20 tests
```

Most useful thing to contribute: **a page where a selector resolves to the wrong
element.** Open an issue with the URL and the output of
`zerodom <url> --html report.html`.

---

## License

Apache 2.0 — see [LICENSE](LICENSE). Use it anywhere, including commercially;
embed it in your own product or framework. ZeroDOM is a trademark of Vexra Labs.
