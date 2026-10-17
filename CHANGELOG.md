# Changelog

All notable changes to ZeroDOM are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.1] — 2026-10-17

Docs and packaging polish; no code or API changes.

### Changed

- Rewrote the PyPI readme (`README.pypi.md`) to lead with the cross-identity
  IDOR / broken-access-control story and the live demo, matching the main README.
- npm releases now publish with `--provenance` (the repo is public), adding a
  verified-provenance badge to the npm package page.
- Broadened the agent-facing `SKILL.md` description to surface the IDOR /
  authorization-testing use-case alongside browser perception.

## [0.1.0] — 2026-10-15

The release leads with the challenge-clearance ladder — walls are cleared, not
handed off — plus deep authenticated recon over the operator's real Chrome,
dynamic-component correctness, and first-run / stability hardening for a
hands-off release window. No breaking API changes (still pre-1.0).

### Added

- **Challenge-clearance ladder — walls are now cleared, not handed off.** The
  old detect-and-hand-off rule is reversed.
  A four-rung ladder runs at the three navigation choke points (`cli._goto`,
  `mcp_server._goto`, `cdp_pipe.outer_html`): reuse carried `cf_clearance`
  clearance → stealth hygiene on the `--stealth` transport (no more
  `Runtime.enable`/`Runtime.evaluate` over the CDP pipe — the mid-load signal
  bot vendors fingerprint) → bundled solver adapters (CapSolver / 2Captcha /
  Anti-Captcha / any custom `ZERODOM_SOLVER_URL` endpoint; opt-in, scope-gated
  like every action, `--no-solver` / `ZERODOM_NO_SOLVER` refuses, spend logged)
  → relay hand-off to the operator. Success lands in
  `metadata["cleared"]` (rung, wall kind, attempts, seconds, spend); a wall no
  rung clears keeps the honest `metadata["blocked"]`. CLI gains `--no-solver`
  and `zerodom config set/get/list` (`~/.zerodom/config.json`, env wins, keys
  never printed).
- **`benchmarks/benchmark_clearance.py`:** per-rung clearance rate against a
  deterministic local fixture wall plus the authorized live wall list
  (nowsecure.nl, turnstile/reCAPTCHA/hCaptcha demos, `ZERODOM_E2E=1`); writes
  `benchmarks/CLEARANCE.md`. `tests/test_clearance.py` covers rung order, the
  kill switch, scope refusal, the fake-provider solve flow, MCP surfacing, and
  the cdp_pipe no-`Runtime.*` regression.
- **Relay-backed crawl (Phase 1):** `zerodom crawl --relay` and the
  `zerodom_crawl` MCP tool walk the operator's real, logged-in Chrome instead of
  a fresh headless profile — same BFS, scope, deny regex and JSONL records;
  challenge-interstitial nodes are recorded as `blocked` and not descended
  into. `crawl` has no stealth transport (the CDP pipe is fetch-only) and says
  so. Live-identity replay (Phase 2) now routes through an in-page credentialed
  `fetch` in the attached tab, so the request leaves Chrome's own network
  stack; registered second identities still use the HTTP client and their
  docstrings no longer claim Chrome's fingerprint. Scope (Phase 4): `eval_js`
  refuses obvious cross-origin navigation when a scope is locked, any
  navigating tool re-asserts the final post-redirect URL, and
  `tests/test_scope_escape.py` table-drives every escape vector to refusal.
- **Dynamic-component correctness (Phase 3):** `_act`'s pre-action identity
  check re-resolves a re-read node by *identity* (tag + input_type + label),
  not by its positional id, so reorder churn can never mis-click — zero or
  ambiguous matches refuse with "page changed under us". Closed shadow roots
  surface as `metadata["closed_shadow_hosts"]` so coverage isn't overstated.
  `zerodom-demo/` is now a fixture app (virtualized churn, open shadow root,
  delayed hydration) with a Chromium-gated CI suite
  `tests/test_dynamic_components.py`.
- **`benchmarks/benchmark_session.py` / `benchmark_crawl.py`:** session-reuse
  (headless/stealth/relay) and crawl-coverage (routes+APIs recall, framed
  against the OWASP WSTG recon checklist) benchmarks; write `SESSION.md` /
  `CRAWL.md`. Relay legs skip honestly when no relay is listening — they never
  spawn one or close the operator's browser. Crawl now uses a desktop viewport
  (1920×1080) so responsive navs are in their expanded state.
- **`zerodom setup`** — a guided first-run: checks for Chromium, prints the unpacked
  extension path and the `chrome://extensions` steps, emits the MCP config JSON for
  Claude Desktop / Cursor, and reports relay reachability. Print-and-guide, no account,
  no telemetry.

- **`zerodom sweep URL --as A=a.json --as B=b.json`** — the one-command
  cross-identity sweep: crawl the app as one identity to map its GET surface, then
  replay every in-scope URL under all identities and report the deterministic
  access-control tells — `cross-identity-identical-response` (byte-identical 2xx
  body = cross-tenant IDOR) and `access-boundary-split` (2xx vs 401/403 = broken
  access control if the only difference is client-controllable). Read-only (GET-only
  replay, destructive URLs dropped); `--format jsonl|md`. Thin orchestration over the
  existing `crawl` + `compare` primitives; regression-tested.
- **Community rule packs (`surfaces/`)** — Nuclei-style, PR-able YAML detection
  packs (`secrets.yaml`, `admin-surfaces.yaml`) loadable via `--rules`, with a
  "write a detection in 5 lines" guide (`surfaces/README.md`, `CONTRIBUTING.md`).
  `tests/test_surfaces.py` loads every pack so a malformed rule fails CI.
- **`zerodom scan --format md`** — emit findings as a GitHub-flavoured Markdown
  table (pipes/newlines in a cell escaped) that pastes straight into a HackerOne
  report, GitHub issue or Jira ticket. Default stays `jsonl` (pipe-friendly).
- **Named, sandboxed tab groups (relay mode).** Six MCP tools —
  `zerodom_create_group` / `list_groups` / `use_group` / `close_group` /
  `adopt_group` / `restore_groups` — give the agent named, colored Chrome tab
  groups that persist across a browser restart (groups and their tabs). Once the
  agent owns a group, the extension refuses to attach to any tab outside its
  groups; no groups = sandbox off (backward compatible). The ops ride a dedicated
  `/control` WebSocket on the relay, since they aren't CDP.

### Changed

- **Dependency bounds tightened for the freeze.** `mcp[cli]>=2.2,<3` (a lower bound so a
  resolver can't pick a pre-`MCPServer` release and import-crash the server) and
  `websockets<15` (the relay couples to a server API that shifted across majors). lxml/
  tiktoken/pyyaml stay uncapped for embeddability.
- **`.mcpb` bundle** now declares the correct `uv` server type and pins the current
  version.

### Fixed

- **Missing Chromium is handled on first run.** The CLI installs it once
  ("Installing Chromium (~150MB, first run only)…") and retries when run at a
  terminal; piped/CI runs get the one-line install hint. The MCP server never
  downloads mid-tool-call — it returns `Chromium isn't installed. Run once:
  uvx --from zerodom playwright install chromium`.
- **First-run no longer hangs.** Calling an MCP tool before the extension is connected
  now fails fast (~10s) with a "load and connect the extension" message instead of a
  ~30s opaque timeout; the relay closes an unattached CDP socket cleanly.
- **Sensitive-field fill refusal** now matches the on-page UI exactly (the `passw`
  prefix), so a field like `Passwort`/`passwd` the UI marks sensitive is also refused
  server-side — closing an under-refusal gap.
- **`zerodom audit`** prints the one-line `playwright install chromium` hint instead of a
  raw traceback when Chromium isn't installed.
- **Multi-tab crash recovery** no longer hands back dead tab handles after a restart on
  an attached session.
- **Relay** rejects `Origin: null` (sandboxed-iframe / `file://` page origins), closing a
  residual of the page-origin connection guard.
- **`compare` / `--storage-state` now send every cookie, not just the last.** The
  plain-HTTP fetch built the `Cookie` header with a per-cookie `add_header`, which
  overwrites — so a multi-cookie session (e.g. a role cookie + the session) dropped all
  but the last, silently breaking the two-identity IDOR diff (both identities came back
  identical). Joined into one header; regression-tested.
- **`zerodom_add_identity` no longer forces a browser attach.** A registered identity is
  a pure HTTP client (Playwright `APIRequestContext`); registration used to bootstrap the
  live browser/relay, so it failed in any session without the extension attached (headless
  / CI). It now starts only Playwright. Regression-tested.
- **`zerodom_compare_identities` flags the access-control boundary, not just identical
  bodies.** When responses differ but one identity is authorized (2xx) and another denied
  (401/403), it now calls out broken access control if the only difference between the
  identities is client-controllable (a cookie/header/param) — it previously reported
  "properly isolated", hiding a privilege-escalation tell.

### Docs

- Expanded the Limitations section (static-fetch vs `--render`, unpacked-only extension,
  POSIX-only `--stealth`, single MCP session, `compare`'s two-identity requirement) and
  added an explicit safety-posture note.
- Honest, consistent IDOR positioning across the site — ZeroDOM provides the
  access-control primitives and surfaces the tell; you confirm the bug.
- **Open-core edition.** README now carries the local / accountless / zero-egress
  promise, an editions table (Core = this repo, Apache-2.0; Enterprise = additive
  team/compliance/support), and a Commercial Support section. `SECURITY.md` states
  the egress posture honestly (zero telemetry; outbound only to your target and, if
  opted in, the solver provider — `--no-solver` kills it); `CONTRIBUTING.md`
  formalizes DCO sign-off.
- **`proof/sweep.gif`** — a scripted (vhs) terminal demo of `zerodom sweep` catching
  broken access control with two identities, reproducible from a local fixture
  (`proof/sweep.tape` + `proof/sweep_demo/`); wired into both READMEs.
- **Layered hero + "How it compares" table** across both READMEs: lead with
  deterministic / local / zero-egress / token-lean, with cross-identity
  authorization testing as the flagship use-case ("you confirm the bug," never
  "AI finds bugs"). Pre-empts the real-Chrome/CDP security objection up front.
- **Interception proxies (Burp / Caido / ZAP)** — a dedicated section making the
  existing `--proxy` story discoverable: it's a standard upstream proxy, so
  ZeroDOM's traffic shows up in whichever tool you already hunt in, no plugin
  needed.

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
