# @vexralabs/zerodom

**Deterministic AppSec & AI perception layer — TypeScript edition.**

Terminal-native DOM perception for red teams and AI agents: cut HTML tokens
98.9% (median) and keep selectors out of the context window.

[![npm version](https://img.shields.io/npm/v/%40vexralabs%2Fzerodom)](https://www.npmjs.com/package/@vexralabs/zerodom)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](../LICENSE)

TypeScript port of [ZeroDOM](https://github.com/DevHusnainAi/zerodom)'s core engine —
same interaction graph, same selector guarantees, for the Node/TS agent stack
(LangChain.js, the Vercel AI SDK, Playwright for Node).

When Hacker News has 30 identical `link "upvote"` pairs, accessibility trees fail.
ZeroDOM assigns 1:1 deterministic handles, resolving `[45]` to the exact DOM
element while keeping CSS selectors entirely out of the context window.

## Install

```bash
npm install @vexralabs/zerodom
```

## Quickstart

```ts
import { ZeroDOM } from "@vexralabs/zerodom";

const graph = await ZeroDOM.fromPage(page);   // any Playwright Page
console.log(graph.toCompactText());           // what you send the model
const selectors = graph.selectorMap();        // { node_01: "#email-input", ... } — stays your side
```

```ts
import { parseHtml } from "@vexralabs/zerodom";

const graph = parseHtml(html, url);           // parse HTML you already have, no browser needed
```

Real output, 11,882 tokens of Hacker News → 2,326:

```text
PAGE: Hacker News | https://news.ycombinator.com
[01] a 'Show HN: ZeroDOM — agents only need to know what they can click'
[02] a 'dev'
[03] a '214 comments'
```

## Core features

- **Deterministic parse, no LLM in the loop.** lxml-equivalent engine on linkedom,
  identical output every run. Sub-50ms on most pages.
- **Selectors never enter the context window.** The model sees `[03]`; the CSS path
  stays in `selectorMap()` on your side.
- **Shadow DOM handled.** Open shadow roots are parsed and light-DOM selectors are
  scoped with `:light(…)`. Pages with no shadow root pay nothing.
- **Structural action diffs.** `+` appeared, `-` gone, `~` value changed — agents
  stop re-reading entire pages.
- **iframe support.** Pass `{ frames: true }` to read same- and cross-origin frames.

## API

```ts
import { ZeroDOM, parseHtml } from "@vexralabs/zerodom";

// From a Playwright page (any object with content()/url()):
const graph = await ZeroDOM.fromPage(page);

// From raw HTML:
const graph = parseHtml(html, url);

// What to send the model:
graph.toCompactText();                    // "[01] a 'Sign In'\n[02] input 'Email'..."
graph.toCompactText({ selectors: true }); // includes CSS selectors
graph.toCompactText({ hrefs: true });     // includes link destinations

// What stays on your side:
graph.selectorMap();  // { node_01: "#email-input", ... }

// Metadata:
graph.metadata;  // { page_title, url, total_interactive_nodes, parsing_latency_ms, warning? }
```

## Why linkedom, not jsdom

`linkedom` gives a real `querySelector`/`getElementById` DOM — needed so every
generated selector can be self-verified, the same guarantee the Python version
makes against a real browser — at a fraction of jsdom's footprint. One caveat this
port works around: unlike jsdom or a real browser, linkedom's parser doesn't
normalize a bare HTML fragment into a full `<html><body>` document, so
`ZeroDOMParser` wraps non-document input itself before handing it to linkedom
(`page.content()` output is unaffected — it's always a full document already).

## Development

```bash
npm install
npm run build   # tsc -> dist/
npm test        # node's built-in test runner, against the built dist
```

## License

Apache 2.0 — see [LICENSE](../LICENSE).
