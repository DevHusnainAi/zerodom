# Changelog

All notable changes to ZeroDOM are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.0.8] — 2026-09-23

Repositioned as a deterministic AppSec & AI perception layer: terminal-native
DOM perception for red teams and AI agents.

### Added

- **`zerodom scan`:** runs a deterministic YAML ruleset (`surfaces.yaml`, or
  your own via `--rules`) over the parsed graph and emits JSONL findings:
  forms without an anti-forgery token, password inputs on pages with no CSRF
  field, exposed admin/internal/debug links, sensitive-looking inputs.
  `--fail-on-finding` for CI. Rules are fixed match keys, never evaluated code.
- **`--stealth`:** spawns a throwaway-profile Chrome and speaks CDP over an
  inherited pipe (FD 3/4). No localhost debugging port, nothing left on disk.
  POSIX only.
- **`inspect --pipe`** streams nodes as JSONL; `-` reads URLs from stdin, so
  `inspect` and `scan` drop into `httpx` / `jq` pipelines.
- **`zerodom extension`:** the Chrome extension now ships inside the wheel;
  this prints its path for "Load unpacked". Each release also attaches
  `zerodom-extension-<version>.zip` with a `.sha256`.
- **Extension popup:** Session, Graph, Network, Cookies, Tools, HUD and About
  tabs. The Graph tab and the in-page sidebar render the same shared
  `graph_ui.js`: type filters, search, severity and selector-health dots,
  snapshot/compare, copy-as-code.
- **Top bar:** segmented bar (Target, Path, Tokens, Step) replacing the
  single-line "driving this tab" banner. Reserves viewport space, pushes
  fixed/sticky headers down (shadow-DOM aware, re-applied on SPA route
  changes), and tracks `pushState`/`replaceState` navigation.
- **Token savings, two baselines:** the bar shows savings vs raw HTML, the
  sidebar vs Playwright's `aria_snapshot(mode="ai")`. Both are `chars ÷ 4`
  estimates.
- **Emergency Stop** in the popup's Session tab: calls
  `chrome.debugger.detach()` directly, with no dependency on the relay or MCP
  server being responsive.
- **Sensitive-field guard:** on attached sessions, `fill` is refused on
  password/card/CVV/SSN fields before anything is dispatched.
- `examples/mcp_agent_system_prompt.md`: a recommended system prompt for
  agents driving the live-browser tool set.

### Changed

- `viewport_only` and `collapse_duplicates` are on by default in
  `ZeroDOM.from_page`.
- Package descriptions and keywords now describe the AppSec / red-team use;
  "web-scraping" dropped.
- New slashed-zero logo, extension icons and banner.

### Security

- **Relay rejects page origins:** WebSocket connections carrying an
  `http://` or `https://` `Origin` are closed, so a website you visit can't
  drive your logged-in Chrome through `127.0.0.1:8765`. The extension
  (`chrome-extension://`) and non-browser clients (no `Origin`) still connect.
- Graph UI: copy-as-code snippets escape backslashes, and the severity flag
  catches `vbscript:` and non-HTML `data:` hrefs and ignores leading
  whitespace before the scheme.

### Fixed

- `context.new_cdp_session()` over the relay: each session now gets a
  distinct id, fixing a Playwright driver crash on the next page action.
- `zerodom_resize_window` over the relay: `Browser.setWindowBounds` is
  translated to `chrome.windows.update`.
- Injected UI (bar, sidebar, log panel) no longer leaks into the interaction
  graph.
- The bar's logo no longer vanishes on CSP-strict sites: inline SVG replaces
  a `data:` URI.

### Performance

- `page.aria_snapshot(mode="ai")` is cached per URL, recomputed only on
  navigation instead of on every action.

## [0.0.7] — 2026-09-19

### Added

- `zerodom_resize_window`: real browser-window resize.

## [0.0.6] — 2026-09-19

### Added

- Multi-tab support: `zerodom_new_tab`, `list_tabs`, `switch_tab`,
  `close_tab`, grouped into one tab group.
- New tools: hover, press_key, drag, upload_file, screenshot, set_viewport,
  eval_js, get_cookies, network_log, status.
- Visible "driving this tab" lock layer: `Input.setIgnoreInputEvents` over a
  raw CDP session, with a cyan border, banner and cursor.

### Fixed

- Relay race between `create_target()` and `chrome.tabs.onCreated`.

## [0.0.5] — 2026-09-19

### Added

- `zerodom relay` CLI command.

### Fixed

- Four relay/extension bugs found in live testing against real Chrome.

## [0.0.4] — 2026-09-18

### Added

- ZeroDOM's own Chrome extension (`manifest.json`, `background.js`, popup).
- Python relay server bridging Playwright's CDP to the extension's
  `chrome.debugger`.
- Highlight pulse and on-page activity log.
- `ZERODOM_CDP_ENDPOINT` to attach to an existing browser.

## [0.0.3] — 2026-09-17

### Changed

- License switched from BUSL-1.1 to Apache-2.0.

## [0.0.1] — 2026-09-17

### Added

- Initial release: parser, CLI, benchmark harness, audit tool.
