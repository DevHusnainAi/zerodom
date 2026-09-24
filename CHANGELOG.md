# Changelog

All notable changes to ZeroDOM are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.0.9] — 2026-09-24

Bug-bounty / red-team workflow: CLI recon + IDOR primitives, an MCP cross-identity
hunting loop, and a relay hardened so it no longer wedges.

### Added

- **Scope enforcement (MCP):** `zerodom_set_scope(hosts, deny, max_rps)` — or the
  `ZERODOM_SCOPE` env (a locked YAML the agent can't widen) — constrains the hunt in
  code: navigation, replay and clicks to an out-of-scope host are refused, destructive
  URLs/controls (logout/delete/deactivate/…) are refused, and requests are throttled to
  the program's rate limit. Makes the agent safe to run unattended.
- **Hunting methodology (`examples/bug_bounty_hunt.md`):** a playbook that orchestrates
  the tools into an autonomous authenticated hunt — recon → test IDOR / access-control /
  business-logic → report, scoped per program, authorization and read-only rules first.
- **`zerodom crawl` — deep authenticated recon.** BFS-walks a rendered app (real JS
  SPAs load) from a start URL, staying in scope, and yields the attack-surface map as
  JSONL: each page's forms (+ CSRF fields), in-scope links, and the API calls its JS
  fires. Read-only and safe to run unattended — never submits a form or follows a
  destructive link (logout/delete/…, configurable via `--deny`). Takes `--storage-state`
  for auth and `--proxy` for Burp. This is the map a hunter builds by hand.
- **Cross-identity hunting loop (MCP):** `zerodom_add_identity` registers a second
  logged-in identity (storage_state cookies and/or an auth header); `zerodom_replay`
  is Burp Repeater for the agent (replay/tamper a request under any identity); and
  `zerodom_compare_identities` fetches one URL as each identity and flags a
  byte-identical response as a cross-tenant IDOR. Runs on the real, rendered session
  (Playwright APIRequestContext), so it works where a plain fetch hits a WAF or login wall.
- **`zerodom compare URL --as NAME=STATE.json …`:** fetch one URL under two saved
  sessions and diff the responses — the cross-tenant IDOR primitive. Byte-identical
  bodies across two identities flags a likely IDOR; `only_<name>` lists the
  actionable nodes each identity sees that the other doesn't (privilege diff).
  Reads a URL list from stdin (`-`) for scripted object-id sweeps.
- **`--proxy URL` / `$ZERODOM_PROXY` + `--insecure`:** route every request through
  an intercepting proxy (Burp/Caido), so ZeroDOM's traffic shows up in the tool
  you already hunt in. Works on the stealth, render, and plain-HTTP paths;
  localhost targets are forced through too.
- **`scan --js`:** a deterministic pass over inline JS for leaked secrets
  (AWS/Google/Stripe/Slack/GitHub keys, private keys, JWTs — reported redacted,
  never reprinted) and interesting endpoints (`/api`, `/admin`, `/internal`,
  `/graphql`).
- **`--header 'K: V'`** (repeatable): send a program's WAF-bypass token.
- **`--storage-state STATE.json`:** reuse a human-cleared session — carry a
  `cf_clearance` cookie / storage state into automated reads.
- **Challenge detection:** Cloudflare, Turnstile, reCAPTCHA and hCaptcha are
  detected and reported as a `blocked` signal (in graph metadata and the CLI
  report) instead of an empty graph. ZeroDOM never solves them; it points at
  relay mode, `--storage-state`, or a bypass header.

### Fixed

- **Relay no longer wedges on a `chrome://` tab.** An un-attachable tab (chrome://,
  the Web Store, devtools, an extension page) — whether opened in the background or by
  the agent — used to fail `chrome.debugger.attach` mid-handshake and kill the whole
  session. Un-attachable tabs are now skipped (auto-attach), refused with a clean error
  (`create_target`), and rejected up front by the MCP tools with a clear message.
- `--frames` combined with `--stealth` now errors instead of silently ignoring
  `--stealth` (they use different fetch engines).
- The render/frames path no longer hangs 30s on a Turnstile/Cloudflare page whose
  network never goes idle; it commits on DOM-ready and bounds the settle wait.
- **Relay recovers from a `chrome.debugger` detach instead of wedging.** A
  navigation/redirect (or DevTools, or a tab replacement) could detach the debugger
  while the relay↔extension socket stayed up — so `zerodom_status` read `attached=True`
  but every command failed opaquely on a dead page. Now: `status` does a real liveness
  probe (`page.evaluate`) and reports whether the tab actually responds; `_page()`
  detects a closed page and reconnects; and the relay re-attaches on a recoverable
  detach (skipping permanent cases — tab closed, DevTools, user-cancelled).
- **First-contact robustness — one clean line, never a traceback:** malformed/truncated
  HTML now yields an empty graph (not an lxml crash); a missing Chromium, a dead
  host/typo (single URL *and* mid-batch, which now skips and continues), and a bad
  `--rules` file each print a single actionable message.

### Changed

- Cap the two fast-moving deps — `mcp[cli]<3` and `playwright<2` — so a breaking
  release can't silently break a fresh `pip install zerodom`.

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
