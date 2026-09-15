# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ZeroDOM parses HTML into a token-optimized "interaction graph" — the clickable/fillable
elements an AI agent can act on (`[03] button 'Sign In'`), with CSS selectors kept
server-side and never sent to the model. Deterministic, no LLM in the parse path:
lxml in, graph out, target <50ms even on a 5,000-node page.

## Commands

```bash
uv sync                              # install deps into .venv
uv run pytest                        # full suite
uv run pytest tests/test_parser.py   # parser only (no browser needed)
uv run pytest -k test_name           # single test
uv run playwright install chromium   # required once for browser-backed tests/features
uv run python benchmarks/benchmark_tokens.py   # token-savings report (tiktoken, cl100k_base)
uv build                             # wheel + sdist into dist/
```

CI (`.github/workflows/`) runs `uv sync && uv run playwright install --with-deps chromium && uv run pytest`
on Python 3.10 and 3.12.

`tests/test_playwright.py` is skipped automatically when Chromium isn't installed
(`pytestmark = pytest.mark.skipif(...)` at the top of the file) — `test_parser.py` and
`test_mcp.py` (which uses a `FakePage`) don't need a real browser at all.

## Architecture

Five modules, each independent and named for its FR (functional requirement) in its
docstring — read the module docstring first, it states the design constraint:

- **`parser.py`** (FR-1) — the core engine. `ZeroDOMParser(html, url).parse()` does one
  DFS pass (`_walk`) that prunes non-content subtrees (`script`/`style`/hidden/etc.) and
  collects interactive elements + `<label>` index in the same traversal. Selector
  generation (`_selector`/`_path`) picks the cheapest CSS selector that is *actually
  unique in the parsed document* — `#id` → `[name]` → single unique class → structural
  child-combinator path anchored at the nearest ancestor `#id`. Uniqueness is checked
  against a one-pass `Counter` (`_counts`), never assumed from the attribute alone,
  because e.g. a `name` shared by a radio group is not unique. Returns an
  `InteractionGraph` (a `dict` subclass) with `.to_json()`, `.to_compact_text()`
  (the token-dense DSL sent to models), and `.selector_map()` (`node_id -> selector`,
  stays on the caller's side).
- **`label_linker.py`** (FR-2) — resolves each element's human-readable label. Priority
  order matters and is documented at the top of the file: `<label for>` → wrapping
  `<label>` → `aria-labelledby` → `aria-label`/`placeholder`/`alt`/`title` → submit
  input's `value` → adjacent caption text (`Search: <input>` → `"Search"`) → element's
  own text → `name`/`value` → `<img alt>`. `LabelLinker` is constructed once per parse
  and reused per-node so lookups stay O(1).
- **`playwright_wrapper.py`** (FR-3) — thin adapter, `ZeroDOM.from_page(page)`. Detects
  sync vs. async Playwright by checking whether `page.content()` returns an awaitable;
  there is no separate sync/async code path beyond that branch.
- **`mcp_server.py`** (FR-4) — Claude/Cursor MCP server (`zerodom-mcp` entry point).
  Holds one global Chromium session (`_session` dict) — this is deliberate
  (`# ponytail: one global browser session`), not an oversight; don't add multi-session
  support unless concurrent pages are actually needed. Every tool re-reads the live DOM
  through `_read()` rather than re-navigating, so typed form state and scroll position
  survive across actions — `zerodom_parse_url` is the only tool that calls `page.goto`.
  Node ids accepted by `zerodom_click_node`/`zerodom_fill_node` are normalized in
  `_selector()`: `"03"`, `"[03]"`, and `"node_03"` all resolve to the same selector.
- **`report.py`** — browser-facing only (screenshot + self-contained HTML inspector with
  numbered badges over every node). Imported lazily from `parser.py`
  (`InteractionGraph.to_html_report`) specifically to keep `parser.py` free of a
  Playwright dependency for callers that only need the deterministic parse.
- **`cli.py`** — `zerodom inspect <url>` (entry point `zerodom`). `capture()` shares one
  Playwright browser session across screenshot + HTML report generation since both need
  the same live page.

Import order in `__init__.py` matters for avoiding circular imports:
`label_linker` → `parser` → `playwright_wrapper`.

## Conventions specific to this codebase

- Every non-obvious selector/label decision has a comment explaining *why*, usually
  anchored to a concrete real-world page (Hacker News numeric ids, browser-injected
  `<tbody>`, etc.) — when changing selector or label logic, check whether the existing
  comment's reasoning still holds before removing it, and check `README.md`'s "Design
  notes" section, which documents the same set of edge cases from the user's side.
  `tests/test_parser.py` has one test per such edge case (e.g.
  `test_table_rows_get_the_tbody_browsers_inject`, `test_numeric_and_odd_ids_use_attribute_selectors`) — if you touch `_selector`/`_path`/`css_id`/`class_token`, run that
  file and check whether a new case needs the same treatment.
- Comments prefixed `# ponytail:` mark a deliberate scope limit, not a bug — see
  `mcp_server.py`'s single-session comment for the pattern. Don't "fix" these without
  checking whether the limitation is intentional.
- No LLM/network calls belong in `parser.py` or `label_linker.py` — the parse must stay
  deterministic and dependency-free of a browser. Browser-only code lives in
  `playwright_wrapper.py`, `report.py`, `mcp_server.py`, or the `--render`/`capture` path
  of `cli.py`.
