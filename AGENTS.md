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
npm test                             # vitest, mirrors tests/test_parser.py's cases
```

## Test suite structure
- `test_parser.py` — deterministic parser tests, no browser needed
- `test_mcp.py` — MCP server tests using `FakePage`
- `test_playwright.py` — browser-backed, skipped if Chromium not installed
- `test_relay.py` / `test_relay_server.py` — relay/extension protocol tests
- `test_mcp_cdp.py` — MCP + CDP integration tests
- `test_audit.py` / `test_cli.py` — no browser needed

CI runs: `uv sync && uv run playwright install --with-deps chromium && uv run pytest` on Python 3.10 and 3.12.

## Architecture (7 modules)
- **`parser.py`** (FR-1) — core engine, single DFS pass, unique CSS selector generation
- **`label_linker.py`** (FR-2) — human-readable label resolution, priority order documented at top of file
- **`playwright_wrapper.py`** (FR-3) — thin adapter, detects sync vs async Playwright
- **`mcp_server.py`** (FR-4) — MCP server (`zerodom-mcp` entry), single global session (`_session` dict), deliberate `# ponytail:` scope
- **`report.py`** — browser-facing only (screenshot + HTML inspector), lazy-imported
- **`frames.py`** — iframe traversal, opt-in via `frames=True`
- **`audit.py`** — selector audit against live page
- **`cli.py`** — `zerodom inspect <url>` entry point

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

## Chrome extension (in `extension/`)
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