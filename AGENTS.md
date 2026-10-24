# AGENTS.md — zerodom operational guide

## What this is
ZeroDOM parses HTML into a token-optimized interaction graph — clickable/fillable elements as `[03] button 'Sign In'`, CSS selectors kept server-side and never sent to the model. Deterministic parse, no LLM in path: lxml in, graph out, target <50ms on 5k nodes.

## Commands
```bash
uv sync                              # install deps into .venv
uv run pytest                        # full suite
uv run pytest tests/test_parser.py   # parser only, no browser
uv run pytest -k test_name           # single test
uv run playwright install chromium   # required once for browser-backed tests/features
uv run python benchmarks/benchmark_tokens.py   # token-savings report
uv build                             # wheel + sdist into dist/

# TypeScript port (`@vexralabs/zerodom`, module-for-module mirror of the Python package)
cd js && npm install
npm run build                        # tsc
npm test                             # node --test; parser.test.ts mirrors test_parser.py, + graphUi.test.ts

# CLI
zerodom <url>                        # inspect
zerodom setup                        # guided first-run: Chromium, extension path, MCP config, relay
zerodom relay                        # WS bridge :8765 for Chrome extension
zerodom inspect <url> --pipe         # JSONL node stream
zerodom inspect --stealth <url>      # throwaway Chrome over CDP pipe, no port
cat targets.txt | zerodom scan -     # surfaces.yaml findings as JSONL
zerodom scan --js <url>              # + DOM sinks, callable API surface, header posture, csrf-posture
zerodom crawl <url>                  # routes/forms/API + js_endpoints (method+body) as JSONL
zerodom compare <url> --as a=a.json --as b=b.json   # cross-tenant IDOR
zerodom sweep <url> --as … --rps 20  # crawl one identity, replay across identities (throttled)
zerodom repeat <url> --method POST --body '{…}' --save ev.har   # Repeater + evidence
zerodom probe  <url> --body '{…}' --tamper '{"role":"admin"}'   # mass-assignment diff
zerodom race   <url> --body '{…}' --body '{…}' -n 30 --engine http2   # TOCTOU burst (zerodom[race])
ZERODOM_E2E=1 uv run pytest tests/test_extension_e2e.py -v -s   # opt-in e2e
```
No linter/formatter/type-checker is configured — no ruff/mypy/black in pyproject or CI; don't guess one.
CI: `uv sync && uv run playwright install --with-deps chromium && uv run pytest` on Python 3.10/3.12. CI does **not** run `npm test` — run the JS suite locally after touching `js/` or `graph_ui.js`. Publish: PyPI on a `v*` tag, npm on a separate `npm-v*` tag (the two versions move independently); `security.yml` runs pip-audit/npm audit weekly.

## Test suite
- `test_parser.py` — deterministic parser, no browser
- `test_mcp.py` — MCP server with FakePage
- `test_playwright.py` — browser-backed, auto-skipped if Chromium missing
- `test_relay.py` / `test_relay_server.py` — relay/extension protocol
- `test_mcp_cdp.py` — MCP + CDP integration
- `test_audit.py` / `test_cli.py` / `test_jsintel.py` / `test_surfaces.py` / `test_graph_ui_sync.py` — no browser
- `test_clearance.py` — ladder order, kill switch, solver flow, cdp_pipe hygiene
- `test_scope_escape.py` — table-driven refusal of every scope-escape vector
- `test_dynamic_components.py` — churn/shadow/hydration fixture; Chromium + fastapi/uvicorn gated
- `test_clearance_live.py` — opt-in `ZERODOM_BYPASS_LIVE=1`, headful Chrome + display, never CI
- `test_extension_e2e.py` — opt-in `ZERODOM_E2E=1`, real extension+Chromium, never CI; stop any running `zerodom relay`/`zerodom-mcp` first (extension only dials the fixed port)

## Architecture
- `parser.py` (FR-1) — core DFS, unique CSS selector generation
- `label_linker.py` (FR-2) — label resolution, priority documented at top of file
- `playwright_wrapper.py` (FR-3) — sync/async Playwright adapter; `from_page` runs a bounded DOM-settle wait (`SETTLE`, MutationObserver, quiet 200ms / cap 1200ms, structural+text only) before serializing so a read lands on a quiet DOM, not one mid-churn — `settle=False` to read-exactly-now. Mirror any change in `js/src/playwright.ts` `SETTLE`.
- `mcp_server.py` (FR-4) — MCP server `zerodom-mcp`, single global session `_session`
- `report.py` — browser-facing screenshot/HTML inspector, lazy-imported
- `frames.py` — iframe traversal, opt-in `frames=True`
- `audit.py` — selector audit against live page
- `cdp_pipe.py` — `--stealth` transport: Chrome via `--remote-debugging-pipe`, raw CDP over FD 3/4 NUL-framed, POSIX-only, `close_fds=False` deliberate
- `surfaces.py` + `surfaces.yaml` — `zerodom scan`, deterministic YAML node rules + `header_rules:` (CSP/CORS/cookie posture) + `csrf_posture` correlation; unknown key = load error
- `jsintel.py` — `zerodom scan --js`: secrets, the callable API surface (`extract_endpoints`: method+body), and DOM-XSS sinks (`scan_sinks`, source-reachability ranked). Network-free; the CLI fetches external bundles and passes them in
- `parser._detect_challenge` + `clearance.py`/`solvers.py`/`scope.py` — CF/Turnstile/reCAPTCHA/hCaptcha detection → `metadata["blocked"]`; `clearance.attempt_clearance` then climbs a 4-rung ladder (reused → stealth → opt-in solver → relay human) at 3 choke points (`cli._goto`/`mcp_server._goto`/`cdp_pipe.outer_html`), result via module slot → `metadata["cleared"]`. We never solve a CAPTCHA ourselves: rung 3 solvers (CapSolver/2Captcha/Anti-Captcha/custom URL) are opt-in via `ZERODOM_SOLVER_*`/`zerodom config set`, scope-gated, spend logged; `--no-solver`/`ZERODOM_NO_SOLVER=1` kill switch, default path zero-egress. Authorized targets only; unclearable wall stays `blocked` (honest miss)
- `cli.py` — `inspect/audit/relay/scan/crawl/compare/sweep/repeat/probe/race/extension/setup/config`; `--pipe`, `--stealth`, stdin `-`; engagement flags `--proxy/$ZERODOM_PROXY`+`--insecure`, `--header`, `--storage-state`; `scan --format jsonl|md` (md = report/ticket table); `--frames`+`--stealth` errors; `compare` for cross-tenant IDOR; `sweep URL --as N=state.json … [--rps]` = crawl-as-one-identity then replay every in-scope GET across identities → access-control findings (read-only); `crawl` for attack-surface map (own Chromium, or `--relay`; `crawl --stealth` errors). Act/verify verbs: `repeat` (Repeater + `--save` HAR/.http), `probe --tamper` (mass-assignment diff), `race` (TOCTOU burst; `_race_burst` last-byte-sync `http1` engine, or `Http2Sender`/httpx `http2` via the `zerodom[race]` extra; `race_report`/`probe_finding` shared with MCP)

Import order in `__init__.py` matters: `label_linker` → `parser` → `playwright_wrapper`.

## Conventions
- `# ponytail:` comments = deliberate scope limit, not a bug
- No LLM/network calls in `parser.py` or `label_linker.py` — parse must stay deterministic
- **Hand-kept mirrors — a fix isn't done until every copy has it:** (1) `parser.py`/`label_linker.py` → `js/src/parser.ts`/`labelLinker.ts` + a case in `js/test/parser.test.ts`; (2) browser-side `SERIALIZE` stamping (`playwright_wrapper.py`) → the `data-zerodom-*` reader in `parser.py` *and* `js/src/parser.ts` (the `SETTLE` constant beside it mirrors Python↔TS too); (3) the sensitive-field regex in `_SENSITIVE_FIELD_RE` (`mcp_server.py`) and `SENSITIVE_RE` (`graph_ui.js`) — the human must see exactly the fields the model is refused on
- `zerodom/graph_ui.{js,css}` is canonical; `extension/` holds a byte-identical copy (an MV3 popup can't load files outside its dir). `test_graph_ui_sync.py` fails if they differ — resync with `cp zerodom/graph_ui.js extension/graph_ui.js` (same for `.css`). **Never edit the `extension/` copy.**
- Version from `importlib.metadata.version("zerodom")` (reads pyproject) — never hardcode. `js/package.json`, `server.json`, `mcpb/manifest.json` carry their own hand-bumped copies
- Every non-obvious selector/label decision has a comment anchored to a real page
- `docs/` is gitignored; `docs/DECISIONS.md` numbers product decisions D1+

## MCP server specifics
- Cross-identity hunting: `zerodom_add_identity` / `zerodom_replay` / `zerodom_compare_identities` — byte-identical response = cross-tenant IDOR
- Recon+act over the active session (CLI parity): `zerodom_scan` (sinks/API surface/header posture), `zerodom_repeat`, `zerodom_probe`, `zerodom_race` (in-page `fetch`/`Promise.all` — the browser's own multiplex engine; reuses `cli.race_report`/`probe_finding`). Cookie-flag/CSRF posture is CLI-only (a page can't read its own `Set-Cookie`)
- Entry point `zerodom-mcp`; single global session `_session`; node ids normalized: `"03"`, `"[03]"`, `"node_03"`
- Tools re-read live DOM via `_read()`; form state/scroll persist
- Only `zerodom_parse_url` calls `page.goto`
- Auto-starts `zerodom relay` on :8765 if `ZERODOM_CDP_ENDPOINT` not set
- Scope enforcement via `zerodom_set_scope` / `ZERODOM_SCOPE`: host allowlist, destructive denylist, rate throttle; locked scope can't be widened
- Tab-group sandbox (relay mode): `zerodom_create_group`/`list_groups`/`use_group`/`close_group`/`adopt_group`/`restore_groups` — named, colored, persistent (storage.local) Chrome groups the agent is sandboxed to; once a group exists the extension refuses attach on tabs outside owned groups. Ops ride a `/control` WS (not CDP — Playwright drops non-schema methods); extension is the enforcer
- Undrivable targets refused: chrome:// etc.
- `ZERODOM_PROXY`, `ZERODOM_SCOPE`, `ZERODOM_CDP_ENDPOINT`, `ZERODOM_E2E` env vars

## Chrome extension
- Shipped bundled in wheel and as zip on releases
- MV3 service worker, WS to `zerodom relay`
- Groups zerodom tabs, requests `debugger/tabs/tabGroups/storage/alarms`
- Uses `Input.setIgnoreInputEvents` to block user input while attached
- Connects to fixed `ws://127.0.0.1:8765/extension/local`; no URL field in popup

## Known gotchas
- `test_playwright.py` requires Chromium — skipped automatically if missing
- Relay logs to `~/.zerodom/relay.log`, not DEVNULL
- `chrome.debugger` allows one client per tab — close orphaned tabs
- `display: contents` elements report `checkVisibility()=false` but children render — parser handles this
- `tabindex="-1"` is programmatically focusable only, not interactive
- HTML comments and Lit hydration markers leak into labels if not filtered
- Icon-font PUA glyphs U+E000-F8FF stripped in label_linker
- `<body tabindex="-1">` was misclassified as clickable with raw `<script>` label
- Editing `graph_ui.js` does **not** affect a running `zerodom-mcp` — `_GRAPH_UI_JS` is read at import time, so restart the server; the extension popup re-reads it off disk on each open, so reloading the unpacked extension is enough there

## Live debugging
- `zerodom_status()` — relay reachability, session state, active tab, relay log tail
- Reload extension in `chrome://extensions` after `background.js`/`manifest.json` changes
- Restart Claude Code session to pick up new MCP tools after `mcp_server.py` changes

## When editing parser/label logic
1. Check `README.md` ("How labels are resolved" / "Limitations") and the code's own anchored comments for documented edge cases
2. Run `tests/test_parser.py` — one test per edge case
3. Mirror fix in `js/src/parser.ts` / `labelLinker.ts` and `js/test/parser.test.ts`

## Token-efficient usage
- Prefer `zerodom_find(query)` over re-reading full graph
- Actions return diff, not full graph, except after navigation
- `viewport_only=True` drops off-screen nodes; first line reports skipped count
- `check_occlusion=True` drops nodes covered by modal/banner; off by default due to cost
- Repeated-list-item grouping under `@card "title":` for `<article>`/`<li>`/`<tr>` with 2+ controls
- Avoid pasting full graphs; request partial views/diffs/search
