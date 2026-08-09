---
name: A node is missing from the graph
about: Something visible on the page didn't appear
title: 'Missing node: '
labels: node-missing
---

**URL**

**The element that should have appeared** — paste its HTML if you can

**Checklist** — these cover most reports, and checking saves us both a round trip:
- [ ] It's not inside an `<iframe>` (if it is, try `frames=True` / `--frames`)
- [ ] It's not inside a **closed** shadow root (unreachable by any browser API)
- [ ] It's not drawn on a `<canvas>` (no element exists to emit)
- [ ] The page had finished rendering when parsed
- [ ] `metadata["warning"]` was empty (it names the cause when the graph is nearly empty)

**How you parsed it** — `from_page`, `--render`, MCP server, or a raw HTML string?

<!-- Parsing an HTML *string* has no CSS cascade, so stylesheet-hidden elements
     are emitted as visible and layout-dependent behaviour differs. -->
