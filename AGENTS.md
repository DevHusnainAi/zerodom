# AGENTS.md — zerodom operational guide

## What this is
ZeroDOM parses HTML into a token-optimized "interaction graph" — the clickable/fillable elements an AI agent can act on (`[03] button 'Sign In'`), with CSS selectors kept server-side and never sent to the model. Deterministic, no LLM in the parse path: lxml in, graph out, target <50ms even on a 5,000-node page.

## Commands

```bash
# Python package
uv sync                              # install deps into .venv
uv run pytest                        # full suite
uv run pytest tests/test_parser.py   # parser only (no browser needed)
uv run pytest -k test_name           # single test
uv run playwright install chromium   # required once for browser-backed tests/features
uv run python benchmarks/benchmark_tokens.py   # token-savings report (tiktoken, cl100k_base)
uv build                             # wheel + sdist into dist/

# TypeScript port (in js/)
cd js && npm install
npm run build                        # tsc
npm test                             # node --test --experimental-strip-types, mirrors tests/test_parser.py's cases
```

## Test suite structure
- `test_parser.py` — deterministic parser tests, no browser needed
- `test_mcp.py` — MCP server tests using `FakePage`
- `test_playwright.py` — browser-backed, skipped if Chromium not installed
- `test_relay.py` / `test_relay_server.py` — relay/extension protocol tests
- `test_mcp_cdp.py` — MCP + CDP integration tests
- `test_audit.py` / `test_cli.py` — no browser needed

CI runs: `uv sync && uv run playwright install --with-deps chromium && uv run pytest` on Python 3.10 and 3.12.

## Architecture (modules)
- **`parser.py`** (FR-1) — core engine, single DFS pass, unique CSS selector generation
- **`label_linker.py`** (FR-2) — human-readable label resolution, priority order documented at top of file
- **`playwright_wrapper.py`** (FR-3) — thin adapter, detects sync vs async Playwright
- **`mcp_server.py`** (FR-4) — MCP server (`zerodom-mcp` entry), single global session (`_session` dict), deliberate `# ponytail:` scope
- **`report.py`** — browser-facing only (screenshot + HTML inspector), lazy-imported
- **`frames.py`** — iframe traversal, opt-in via `frames=True`
- **`audit.py`** — selector audit against live page
- **`cdp_pipe.py`** — `--stealth` transport: spawns Chrome via `--remote-debugging-pipe`, raw CDP over FD 3/4 (NUL-framed, sync). No port, no Playwright, ephemeral profile. Spawns fresh (can't attach to running Chrome — that's the relay). POSIX-only, `close_fds=False` is deliberate. Feeds the existing parser.
- **`surfaces.py`** + **`surfaces.yaml`** — `zerodom scan`: deterministic YAML rules over the parsed graph (sensitive labels, admin links, missing-CSRF). Fixed match keys + regex, no expression language. Unknown key = load error.
- **`cli.py`** — `zerodom inspect`/`audit`/`relay`/`scan`; `--pipe` (JSONL nodes), `--stealth`, `-` reads target URLs from stdin

Import order in `__init__.py` matters: `label_linker` → `parser` → `playwright_wrapper`.

## Conventions that differ from defaults
- `# ponytail:` comments = deliberate scope limit, not a bug — don't "fix" without checking intent
- No LLM/network calls in `parser.py` or `label_linker.py` — parse must stay deterministic
- Selector/label logic duplicated by hand in `js/src/` (TypeScript). Fix in Python → must mirror in TypeScript
- Version from `importlib.metadata.version("zerodom")` — never hardcode
- Every non-obvious selector/label decision has a comment explaining *why*, anchored to real-world page

## MCP server specifics
- Entry point: `zerodom-mcp` (console script)
- Single global session (`_session` dict) — node ids normalized: `"03"`, `"[03]"`, `"node_03"` all work
- Tools re-read live DOM via `_read()` — form state and scroll persist across actions
- Only `zerodom_parse_url` calls `page.goto`
- Auto-starts `zerodom relay` on port 8765 if `ZERODOM_CDP_ENDPOINT` not set

## Token-optimization features (discoverability: these live in the tool docstrings
themselves, since that's what a calling agent actually reads — not just here)
- `zerodom_find(query)` — search the current graph instead of re-reading the whole page
- Actions (`click`/`fill`/etc.) return a diff, not the full graph, except after a navigation
- `zerodom_parse_url(url, viewport_only=True)` — drops off-screen nodes on long feeds
  (YouTube home, Reddit, Twitter); sticky for the session like `frames`. Implemented via
  `data-zerodom-offscreen`, the same browser-stamps-a-data-attribute pattern `data-zerodom-hidden`
  already uses — see `_is_offscreen` in `parser.py` / `isOffscreen` in `js/src/parser.ts`. When
  it drops anything, the compact graph's first line says how many, so an agent knows to scroll
  and re-read rather than assume the page is just small.
- `zerodom_parse_url(url, check_occlusion=True)` — drops nodes covered by a modal/dropdown/
  banner (same `data-zerodom-*` stamp pattern, `data-zerodom-occluded`, center-point
  `elementFromPoint()` hit-test in SERIALIZE). Off by default, unmeasured cost — see D16 in
  `docs/DECISIONS.md` (gitignored) for why it wasn't defaulted on.
- Repeated-list-item grouping: nodes under a `<article>`/`<li>`/`<tr>`/`role=article|listitem|row`
  ancestor holding 2+ controls get a `@card "title":` header in `to_compact_text()` —
  `CARD_TAGS`/`CARD_ROLES`/`_card_title` in `parser.py`. Deliberately excludes `<form>` and
  single-control cards; both caused real test regressions (a login form got grouped for no
  reason, a 200-row single-link table doubled its own line count) before being scoped down.
- `zerodom_click_node` flags a same-page, zero-diff click as a possible SSR-hydration miss
  (`_recently_navigated()`, 5s grace after `_goto()`) rather than trying to detect it
  pre-emptively — `element.onclick`/listener-count checks don't work for React/Next.js, which
  delegates one listener at the root and never attaches one to the clicked element itself.
- `<div contenteditable>` (Notion, Slack, Discord, Jira) is a real fillable node
  (`_is_content_editable` in `parser.py`, `node["content_editable"]`), and `zerodom_fill_node`
  routes it through Playwright's `locator.press_sequentially()` instead of `.fill()` — these
  editors run their own state machine off real keystroke events and ignore/mishandle a bulk
  insert. `contenteditable="false"` is deliberately excluded (an inert island, e.g. a mention
  chip, inside an editable ancestor).

## Chrome extension (in `extension/`)
- Shipped two ways: bundled in the wheel (`zerodom extension` prints the path for Load unpacked)
  and as `zerodom-extension-<tag>.zip` + `.sha256` on each `v*` GitHub release (`release.yml`)
- MV3 service worker (`background.js`), communicates via WebSocket to `zerodom relay`
- Auto-connects on startup/install/wake, retries via `chrome.alarms`
- Requests `debugger`, `tabs`, `tabGroups`, `storage`, `alarms` permissions
- Groups all zerodom tabs into one visible "zerodom" tab group (cyan)
- Uses `Input.setIgnoreInputEvents` over raw CDP session to block user input while attached
- No URL field in popup — connects to fixed `ws://127.0.0.1:8765/extension/local`

## Known gotchas
- `test_playwright.py` requires Chromium installed — skipped automatically if not
- Relay auto-spawn pipes stdout/stderr to `~/.zerodom/relay.log` (not DEVNULL)
- `chrome.debugger` only allows one client per tab — close orphaned tabs if "Another debugger already attached"
- `display: contents` elements report `checkVisibility() = false` but children render normally — parser now handles this
- `tabindex="-1"` means programmatically focusable only, NOT interactive — parser treats as non-interactive
- HTML comments (including Lit hydration markers `<!--?lit$...-->`) leak into labels if not filtered
- Icon-font PUA glyphs (U+E000-F8FF) concatenate into visible text — stripped in label_linker
- `<body>` with `tabindex="-1"` was misclassified as clickable with raw `<script>` text as label

## Live debugging
- `zerodom_status()` tool: reports relay reachability, session state, active tab, recent relay log tail
- Reload extension in `chrome://extensions` after any background.js/manifest.json changes
- Restart Claude Code session to pick up new MCP tools after mcp_server.py changes
- `ZERODOM_E2E=1 uv run pytest tests/test_extension_e2e.py -v -s` — opt-in live extension test (requires running relay on default port 8765)

## When editing parser/label logic
1. Check `README.md` "Design notes" for documented edge cases
2. Run `tests/test_parser.py` — one test per edge case
3. Mirror the fix in `js/src/parser.ts` / `labelLinker.ts` and `js/test/parser.test.ts`

## Token-efficient usage for agents
* Prefer `zerodom_find(query)` over re-reading the whole graph when searching.
* Use `zerodom_parse_url(..., viewport_only=True)` on long feeds/infinite scroll pages. The graph first line reports how many nodes were skipped off-screen.
* Enable `collapse_duplicates=True` for feed pages with repeated low-signal controls. The compact text shows `×N` and metadata reports `duplicates_collapsed`.
* Avoid pasting full graphs into context; request partial views, diffs, or search results instead.