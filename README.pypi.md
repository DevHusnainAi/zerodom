# ZeroDOM

### Your agent doesn't need the DOM. It needs to know what it can click.

ZeroDOM turns a bloated HTML page into a token-optimized **interaction graph** —
the buttons, inputs and links an agent can actually act on, and nothing else.

An agent driving a browser gets one of two action spaces today, and both are bad.
**Pixels** are slow, expensive, and produce coordinates that go stale the moment
the page scrolls. **The accessibility tree** is cheaper but enormous, and it has
no stable handles: 102 of Hacker News' 220 actionable nodes share a
`(role, name)` pair with another node, so there is no way to say *which* story to
upvote.

ZeroDOM is a third option — a flat list of what the page can do, where every
entry has a stable id, and the addressing information that makes it clickable
never enters the context window. A median of **10.2 tokens per action across 111
live sites**, where **10,275 of 10,382 selectors resolved to exactly one live
element** and only 2 were ambiguous.

- **No LLM in the loop.** lxml in, graph out, 10–30ms, identical output every run.
- **Selectors never enter the context window.** The model sees `[03]`; the CSS
  path stays in `selector_map()` on your side.
- **Nothing leaves your machine.** No telemetry, no API keys, no storage — the
  only network traffic is the page you pointed it at.
- **The selectors actually resolve.** Verified in a real browser: 231/231 on
  Hacker News, where rows carry no `id` or `class` and numeric ids need escaping.

## Install

```bash
pip install zerodom
# or: uvx zerodom — the CLI runs straight off PyPI
```

One extra step only if you use the browser-backed features (`from_page`,
`--render`, `--screenshot`, `--html`):

```bash
playwright install chromium
```

## Quickstart

```python
from zerodom import ZeroDOM

graph = ZeroDOM.from_page(page)      # any Playwright page, sync or async
print(graph.to_compact_text())       # what you send the model
selectors = graph.selector_map()     # {"node_01": "#email-input", ...} — stays your side
```

Real output:

```text
PAGE: Orbit — Fleet Console | file:///…/demo/demo-page.html
[01] a 'Fleet'
[05] input 'Search'
[07] input* 'Assigned driver' ph='Search by name'
[15] button 'Dispatch'
[17] button! 'Recall (in transit)'
```

`*` marks required, `!` marks disabled. The model answers `click 15`. You resolve
`node_15` against `selector_map()` and click it.

## Cost per action

The fair comparison isn't raw HTML — nobody sends a model raw HTML. It's
Playwright's `page.aria_snapshot(mode="ai")`, which is what Playwright MCP puts
in a model's context.

| page | ZeroDOM | ARIA |
|---|---:|---:|
| airbnb.com | **9.9** | 23.3 |
| github.com/…/issues | **12.1** | 70.2 |
| en.wikipedia.org article | **11.6** | 41.0 |
| news.ycombinator.com | **10.2** | 47.0 |
| developer.mozilla.org | **9.9** | 46.8 |

*tokens per actionable node — measured 2026-08-03*

**A median of ~10 tokens per action**, against ARIA's 23–70 and wildly variable.
Mean 71.0% fewer tokens than the snapshot a model actually gets.

Across **111 live sites** (static, SPA, shadow DOM, iframe, canvas, commerce,
government, forms, login walls), `benchmarks/benchmark_sites.py` measured
**10,382 nodes: 98.97% resolved to exactly one live element, 0.02% ambiguous,
0 invalid**, and 96.1% of a sampled 1,201 were actionable to Playwright. Median
saving vs raw HTML 98.9%, worst case 64.1%. Parse: median 39ms, p90 214ms.

The residual misses are almost all timing, not addressing: a page that is still
hydrating, or a third-party script inserting a wrapper `<div>`, shifts the
structural paths underneath you. Re-tested on a settled DOM, bbc.co.uk goes from
180 misses to 0 and surveymonkey.com from 94 to 0.

## MCP server

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

Goes in `claude_desktop_config.json` for Claude Desktop, or `.cursor/mcp.json`
for Cursor.

| tool | what it does |
|---|---|
| `zerodom_parse_url(url, verbose=False)` | navigate, return the compact graph |
| `zerodom_read_page(verbose=False)` | re-read the live DOM **without navigating** |
| `zerodom_find(query)` | return only the nodes matching a phrase |
| `zerodom_click_node(node_id)` | click, then return **what changed** |
| `zerodom_fill_node(node_id, text)` | type, then return what changed |

An agent loop shouldn't re-read the page it already has. `zerodom_find` answers
"where's the dispatch button?" in one line, and actions return a diff — `+`
appeared, `-` gone, `~` value changed — instead of re-listing every node.

## CLI

```bash
zerodom https://news.ycombinator.com          # compact graph + token savings
zerodom https://example.com --find checkout   # only the nodes that match
zerodom https://example.com --render          # headless Chromium, for JS-heavy pages
zerodom https://example.com --json            # full JSON graph, selectors included
zerodom https://example.com --frames          # also read inside iframes
zerodom https://example.com --html out.html   # graph beside an annotated screenshot
```

## Limitations

- **Closed shadow roots** are unreachable — no browser API exposes them. Open
  roots work.
- **Iframes are opt-in.** `from_page(page, frames=True)`, `zerodom --frames` or
  the MCP tool's `frames=True` reads same- and cross-origin frames and keeps
  every node clickable. It is off by default because it costs a read per frame
  and most frames on a commercial page are advertising.
- **Canvas-rendered UIs** have no DOM to read. Use a vision model there.
- **Stylesheet-hidden controls need a live browser.** With a real page
  (`from_page`, `--render`, the MCP server) the cascade is consulted and hidden
  elements are dropped. Parsing an HTML *string* has no cascade, so
  `<div class="hidden">` is emitted as if visible.
- **Labels come from the page**, so a hostile page can write anything into one.
  Treat graph text as untrusted input — see SECURITY.md.
- **A graph is a snapshot.** Selectors are structural, so a page still
  hydrating — or a third-party script inserting a wrapper `<div>` — can
  invalidate one within seconds. Re-read after each action; the MCP server does
  this for you.
- ZeroDOM operates on a `Page` you already control. It does not bypass bot
  detection and makes no attempt to.

When a graph comes back almost empty, `metadata["warning"]` says why — bot wall,
open modal, or content behind an iframe or canvas — instead of leaving you to
guess whether the page or the parser was at fault.

## Links

- **Source, full README and benchmarks:**
  https://github.com/DevHusnainAi/zerodom
- **Issues:** https://github.com/DevHusnainAi/zerodom/issues
- **License:** Business Source License 1.1 — free to use, including in
  production; you may not offer ZeroDOM itself as a hosted service. Converts to
  Apache 2.0 on 2030-08-09.

