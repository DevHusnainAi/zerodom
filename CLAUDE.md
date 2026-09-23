# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ZeroDOM parses HTML into a token-optimized "interaction graph" — the clickable/fillable
elements an AI agent can act on (`[03] button 'Sign In'`), with CSS selectors kept
server-side and never sent to the model. Deterministic, no LLM in the parse path:
lxml in, graph out, target <50ms even on a 5,000-node page.

`AGENTS.md` is the same guide condensed for other agents — when you change facts here
(commands, module list, conventions), update it too or the two drift.

## Commands

```bash
uv sync                              # install deps into .venv
uv run pytest                        # full suite
uv run pytest tests/test_parser.py   # parser only (no browser needed)
uv run pytest -k test_name           # single test
uv run playwright install chromium   # required once for browser-backed tests/features
uv run python benchmarks/benchmark_tokens.py   # token-savings report (tiktoken, cl100k_base)
uv build                             # wheel + sdist into dist/

zerodom <url>                        # = `zerodom inspect <url>`; also `audit`, `relay`, `scan`
zerodom relay                        # WS bridge on :8765 for the Chrome extension
zerodom inspect <url> --pipe         # JSONL node stream to stdout (one node per line)
zerodom inspect --stealth <url>      # spawn throwaway Chrome over a CDP pipe (no port, no Playwright)
cat targets.txt | zerodom scan -     # match each page against surfaces.yaml, findings as JSONL
ZERODOM_E2E=1 uv run pytest tests/test_extension_e2e.py -v -s   # opt-in, real browser+extension
```

CI (`.github/workflows/ci.yml`) runs `uv sync && uv run playwright install --with-deps chromium && uv run pytest`
on Python 3.10 and 3.12, plus a separate `pip-audit` / `npm audit` job. PyPI publishes on a `v*` tag, npm on a separate `npm-v*` tag —
the two packages version independently.

There is a parallel TypeScript port under `js/` (`@vexralabs/zerodom` on npm) that mirrors
the Python module-for-module (`parser.ts`, `labelLinker.ts`, `playwright.ts`, `frames.ts`).
It has its own toolchain and test suite — changes to selector/label logic in Python have
no effect on it and need the equivalent edit made by hand:

```bash
cd js && npm install
npm run build                        # tsc
npm test                             # node --test --experimental-strip-types, mirrors tests/test_parser.py
```

### Which tests need what

- `test_parser.py`, `test_audit.py`, `test_cli.py`, `test_jsintel.py`, `test_surfaces.py`, `test_graph_ui_sync.py` — pure, no browser.
- `js/test/graphUi.test.ts` — the shared graph UI, mounted under linkedom (`cd js && npm test`).
- `test_mcp.py`, `test_mcp_cdp.py` — MCP tools against a `FakePage`/`FakeGotoPage`, no browser.
- `test_relay.py` — `BrowserModel` state machine with a `FakeExtension`; `test_relay_server.py` —
  real WebSocket round-trips through `RelayServer`, no browser. Each test runs its whole
  start/exercise/stop inside one `asyncio.run()` (a `websockets` server is bound to the loop
  that created it).
- `test_playwright.py` — real Chromium, auto-skipped when it isn't installed
  (`pytestmark = pytest.mark.skipif(...)` at the top of the file).
- `test_extension_e2e.py` — real relay + real unpacked extension + headed Chromium, opt-in via
  `ZERODOM_E2E=1` and never in CI. Stop any running `zerodom relay`/`zerodom-mcp` first: the
  extension only ever dials the fixed default port. Run it manually after touching
  `relay.py` or `extension/background.js`.

## Architecture

Two layers that share one parser: the **deterministic parse** (`parser.py` + `label_linker.py`,
no browser, no network) and the **browser-driving** layer around it (`playwright_wrapper.py`,
`mcp_server.py`, `relay.py`, `extension/`, `report.py`, `cli.py`). Read the module docstring
first — for the FR-numbered ones it states the design constraint.

- **`parser.py`** (FR-1) — the core engine. `ZeroDOMParser(html, url).parse()` does one
  DFS pass (`_walk`) that prunes non-content subtrees (`script`/`style`/hidden/etc.) and
  collects interactive elements + `<label>` index in the same traversal. Selector
  generation (`_selector`/`_path`) picks the cheapest CSS selector that is *actually
  unique in the parsed document* — `#id` → `[name]` → single unique class → structural
  child-combinator path anchored at the nearest ancestor `#id`. Uniqueness is checked
  against a one-pass `Counter` (`_counts`), never assumed from the attribute alone,
  because e.g. a `name` shared by a radio group is not unique. Returns an
  `InteractionGraph` (a `dict` subclass — index it, `graph["metadata"]`, not
  `graph.metadata`) with `.to_json()`, `.to_compact_text()` (the token-dense DSL sent to
  models), and `.selector_map()` (`node_id -> selector`, stays on the caller's side).
  `_walk` also skips zerodom's own injected UI (`[id^="zerodom-"]`, `data-zerodom-ignore`)
  and collects `<input type="hidden">` into `hidden_fields` (metadata, never nodes —
  CSRF/per-view payload an agent may need to submit, but nothing it can click).
- **`label_linker.py`** (FR-2) — resolves each element's human-readable label. Priority
  order matters and is documented at the top of the file: `<label for>` → wrapping
  `<label>` → `aria-labelledby` → `aria-label`/`placeholder`/`alt`/`title` → submit
  input's `value` → adjacent caption text (`Search: <input>` → `"Search"`) → element's
  own text → `name`/`value` → `<img alt>`. `LabelLinker` is constructed once per parse
  and reused per-node so lookups stay O(1).
- **`playwright_wrapper.py`** (FR-3) — thin adapter, `ZeroDOM.from_page(page)`. Detects
  sync vs. async Playwright by checking whether `page.content()` returns an awaitable;
  there is no separate sync/async code path beyond that branch.
- **`mcp_server.py`** (FR-4) — Claude/Cursor MCP server (`zerodom-mcp` entry point), by far
  the largest module; see the section below.
- **`relay.py`** + **`extension/`** — how the MCP server drives the user's *real, logged-in*
  Chrome instead of a fresh headless one. `relay.py` is a local WS server with two endpoints
  (`/cdp/<id>` for our `connect_over_cdp`, `/extension/<id>` for the MV3 service worker),
  started by `zerodom relay` or auto-spawned by the MCP server. `BrowserModel` is a pure
  state machine translating the `chrome.debugger` dialect the extension speaks into the CDP
  dialect Playwright speaks — all its I/O arrives as injected callables, which is what makes
  `test_relay.py` possible without a browser. Protocol is a Python port of Microsoft's
  `cdpRelayV2.ts`; keep the wire format compatible. The extension attaches via
  `chrome.debugger` (not a raw CDP port — see D10), groups zerodom tabs into one cyan tab
  group, and has no URL field by design. `chrome.debugger` allows one client per tab —
  "Another debugger already attached" means an orphaned tab (or DevTools) holds it. Changes
  to `background.js`/`manifest.json` need a reload in `chrome://extensions`.
- **`report.py`** — browser-facing only (screenshot + self-contained HTML inspector with
  numbered badges over every node). Imported lazily from `parser.py`
  (`InteractionGraph.to_html_report`) specifically to keep `parser.py` free of a
  Playwright dependency for callers that only need the deterministic parse.
- **`frames.py`** — iframe traversal, opt-in (`frames=True`) since ads/trackers/consent
  gates are iframes too. `frame_chain()` records the per-node chain of
  `iframe >> nth=N` selectors from the top document down, indexed rather than named
  because most iframes have no stable id/class. `about:blank` is deliberately not
  skip-listed — `srcdoc`/`document.write` frames (CodePen result panels, "run it"
  sandboxes) keep that URL and are exactly the content an agent came for; a frame is
  judged by size/content, not its URL.
- **`cdp_pipe.py`** — the `--stealth` transport. Spawns Chrome with
  `--remote-debugging-pipe` and talks raw CDP JSON-RPC over inherited FD 3
  (commands in) / FD 4 (events out), NUL-framed, synchronous (one outstanding
  command, events buffered on `self.events`). No TCP port, no Playwright driver,
  ephemeral `--user-data-dir`. It *spawns* a fresh Chrome — it cannot attach to
  your running browser (a pipe's FDs are fixed at launch; that's the relay's
  job). POSIX-only: `close_fds=False` on the Popen is deliberate (with it on,
  CPython's fd-close scan reclaims slot 4 and Chrome reports "pipe fds not
  open"); Windows raises. Feeds the *existing* `ZeroDOMParser`, not a new one —
  it just fetches `outerHTML` after load. Also carries the engagement options for
  the stealth path: `proxy`/`insecure` become `--proxy-server`/`--ignore-certificate-errors`
  Chrome args (with `--proxy-bypass-list=<-loopback>` so a localhost target still
  goes through Burp/Caido); `headers`/`cookies` are set over CDP
  (`Network.setExtraHTTPHeaders`/`setCookies`) in `outer_html` before navigating.
- **`surfaces.py`** + **`surfaces.yaml`** — `zerodom scan <url> [--rules PATH]`.
  Runs deterministic client-side rules over the parsed graph (sensitive input
  labels, admin/debug links, forms/passwords with no CSRF hidden field). Not an
  expression language by design — a rule is fixed match keys (exact string, or a
  named-field regex) ANDed, plus page-level CSRF-token gates; `yaml.safe_load`
  in, `re.search` out, nothing to sandbox because nothing is evaluated. Unknown
  match keys are a load-time error, not a silent no-op. Complements `audit.py`:
  that checks selectors a human wrote, this checks the surface the parser found.
- **`jsintel.py`** — `zerodom scan --js`. Deterministic regex over a page's inline
  `<script>` blocks + markup for leaked secrets (AWS/Google/Stripe/Slack/GitHub
  keys, private keys, JWTs — matched value redacted in the finding so the log
  isn't a fresh copy of the leak) and interesting endpoints (`/api|/admin|/internal|
  /graphql` path literals, from script text only so hrefs/visible links don't
  count). Reuses `surfaces.Finding` so findings stream through the same JSONL
  path. Linked/bundled JS is not fetched — that's a per-script network fan-out,
  its own feature. Off unless `--js`.
- **Blocked-state detection** (`parser._detect_challenge`) — a deterministic
  substring match over the raw HTML for CF/Turnstile/reCAPTCHA/hCaptcha
  fingerprints, surfaced as `metadata["blocked"] = {"kind", "marker"}`. We detect
  and hand off, never solve; the honest paths (relay mode, `--storage-state`
  clearance reuse, program bypass header) are the answer, not an evasion engine.
  Markers verified live 2026-09-23 (see `docs/PROFILE-A-PLAN.md`).
- **`audit.py`** — `zerodom audit <dir> --url <url>`, no ZeroDOM required in the suite
  being audited. Regexes (`CALL_PATTERNS`) pull selectors out of existing Playwright/
  Puppeteer/Cypress/Selenium test code, then resolves each against a live page and
  reports `ok`/`dead`/`ambiguous`/`invalid` — the same "matches more than one element"
  failure mode `_selector()` in `parser.py` is built to avoid, but for selectors a human
  already wrote.
- **`cli.py`** — `zerodom inspect <url>` / `audit` / `relay` / `scan` / `compare` / `extension` (prints the unpacked
  extension's path; the wheel bundles `extension/` as `zerodom/extension` via hatch `force-include`,
  and each `v*` release also attaches a zip + sha256) (entry point `zerodom`; a bare
  first arg that isn't a subcommand is rewritten to `inspect`). `capture()` shares one
  Playwright browser session across screenshot + HTML report generation since both need
  the same live page. `inspect`/`scan` share engagement flags via `_add_fetch_flags`:
  `--proxy`/`$ZERODOM_PROXY` + `--insecure` (Burp/Caido), `--header 'K: V'` (repeatable,
  a program's bypass token), `--storage-state STATE.json` (reuse a cleared session —
  native storage_state on the Playwright paths, cookies via CDP on stealth, Cookie
  header on plain HTTP). `--frames`+`--stealth` errors rather than silently dropping
  stealth; `_goto()` commits on `domcontentloaded` then bounds the `networkidle`
  wait so a Turnstile/CF page can't hang the fetch.
  `compare_identities()` (`zerodom compare URL --as NAME=STATE.json …`, ≥2) fetches one
  URL under each identity's cookies and diffs — byte-identical bodies across two
  identities is the cross-tenant IDOR tell; `only_<name>` lists the actionable
  nodes each identity sees that the other doesn't. `_http_fetch` returns the
  status (403-vs-200 is the point) where `fetch` drops it.
  `crawl_site()` (`zerodom crawl URL`) is the recon crawler: a rendered BFS over
  same-scope routes that records each page's forms, in-scope links and fired
  XHR/fetch API calls (via `page.on("request")`), read-only (skips a `--deny` regex of
  destructive links). It's sync Playwright, so tests run it in a worker thread to dodge a
  leaked asyncio loop; in production it's its own process.

Import order in `__init__.py` matters for avoiding circular imports:
`label_linker` → `parser` → `playwright_wrapper`.

### `mcp_server.py` specifics

- One global Chromium session (`_session` dict) — deliberate
  (`# ponytail: one global browser session`), not an oversight; don't add multi-session
  support unless concurrent pages are actually needed. Multi-*tab* already exists
  (`_session["pages"]`, `zerodom_new_tab`/`list_tabs`/`switch_tab`/`close_tab`).
- Every tool re-reads the live DOM through `_read()` rather than re-navigating, so typed
  form state and scroll position survive across actions — `zerodom_parse_url` is the only
  tool that calls `page.goto`.
- Node ids are normalized in `_selector()`: `"03"`, `"[03]"`, and `"node_03"` all resolve
  to the same selector.
- **Cross-identity hunting loop** (the IDOR/authz weapon): `zerodom_add_identity(name,
  storage_state, header)` registers a second logged-in identity as a Playwright
  `APIRequestContext` in `_session["identities"]`; `zerodom_replay(url, method, body,
  header, as_identity)` is Burp Repeater for the agent; `zerodom_compare_identities(url,
  …)` fetches one URL as each identity and flags a byte-identical response as a
  cross-tenant IDOR. Uses the request context (real session cookies), so it works past a
  WAF/login wall where a plain fetch can't. `_reject_undrivable()` refuses chrome://,
  extension, devtools and Web Store URLs (they can't be attached and used to wedge the
  session); `relay._is_attachable()`/`create_target` enforce the same at the relay layer.
- **Scope enforcement** (`_scope_ok`/`_enforce_action`, tool `zerodom_set_scope`, env
  `ZERODOM_SCOPE`): `_session["scope"]` = {allow host globs, deny regex, max_rps, locked}.
  Wired into `zerodom_parse_url`/`zerodom_new_tab`/`zerodom_replay`/`zerodom_compare_identities`
  (URL host allowlist + destructive denylist + throttle) and `_act` (destructive click/fill
  refused). None = unrestricted (backward compatible). A file-loaded scope is `locked` so
  the agent can't widen its own bounds — this is what makes an unattended hunt safe.
- Connection: `ZERODOM_CDP_ENDPOINT` wins if set; otherwise `_ensure_relay_running()`
  auto-spawns `zerodom relay` on :8765 and points at it. Relay output goes to
  `~/.zerodom/relay.log` (not DEVNULL — a stuck handshake is invisible otherwise).
  `zerodom_status` is the diagnostic: relay reachability, attach state, log tail.
- Token-frugality behaviours live in the *tool docstrings*, because that's what a calling
  agent actually reads: `zerodom_find(query)` searches the existing graph instead of
  re-reading the page; actions return a diff rather than the whole graph except after a
  navigation; `viewport_only` / `check_occlusion` / `collapse_duplicates` are sticky
  per-session flags; `@card` grouping collapses repeated list items.
- `_DRIVING_UI_JS` injects the on-page driving bar and mounts the shared graph UI (see
  below) — it is not a lookalike of the popup, it runs the same file. Injected UI must
  carry `data-zerodom-ignore` / an `id` starting `zerodom-` so the parser excludes it;
  otherwise the sidebar ends up in its own graph, and the page-click capture self-refers.
- Both graph UIs exist only while `_session["attached"]` is true (the relay/extension
  driving path). A plain headless session injects no bar, no sidebar, and pushes no
  `__zerodomNodes` — so the popup's Graph tab is empty too. See the next section.
- **`window.__zerodomNodes` is the only contract between the MCP server and the extension
  popup.** `_SIDEBAR_DATA_JS` writes the node list (plus `__zerodomCompTokens`/
  `__zerodomAriaTokens`) into the page on every attached `_read()`; `extension/background.js`
  reads it back over `chrome.debugger` (`GRAPH_JS`, `ensureClickCapture`, `drainClicked`).
  The popup never talks to the Python process — so a field the popup needs (`role`,
  `input_type`, `placeholder`, `disabled`, `required`, `content_editable`, `href`) has to be
  added to that payload in `_read()` first, or it silently reads as empty.

### The graph UI, and its two hosts

`zerodom/graph_ui.js` + `graph_ui.css` are the graph browser — chips, filters,
grouped/flat views, severity and stability dots, token badges, snapshot/compare, bulk
select, code panel, context menu. **One file, rendered in two places**, so they cannot
drift: the extension popup's Graph tab and the in-page sidebar `_DRIVING_UI_JS` injects.

`create(root, host)` renders its own markup and takes every page operation from `host`,
because that is the only thing the two differ in:

| | in-page sidebar | popup Graph tab |
|---|---|---|
| Mounts into | a **shadow root** on `#zerodom-graph-host` — the CSS uses generic names (`.chip`, `.graph-node`) that would collide with a real site's stylesheet both ways | the popup document |
| Gets nodes by | direct push from Python — `_SIDEBAR_DATA_JS` → `__zerodomUpdateGraph` | pull over `chrome.debugger` — `GRAPH_JS` reads `window.__zerodomNodes` |
| `highlight`/`act`/`validate` | direct DOM calls; it's already in the page | `chrome.runtime.sendMessage` → `background.js` → CDP, so `validate` batches the whole graph into one `Runtime.evaluate` |
| `drainClicks` | a local array filled by its own capture listener | `ensureClickCapture()` + `drainClicked()` round-trips |
| Snapshots | a page variable — dies on reload (`ponytail:` comment says why) | `chrome.storage.local`, per-site, durable |

Two copies exist **on disk** (`zerodom/` and `extension/`) because the MCP server loads the
`zerodom/` copy and an MV3 popup can't load a file from outside its own directory.
`tests/test_graph_ui_sync.py` fails if they differ; resync with
`cp zerodom/graph_ui.js extension/graph_ui.js` (same for the `.css`). Never edit the
`extension/` copy.

`js/test/graphUi.test.ts` mounts the module under linkedom — the only DOM in the repo — and
is where a render regression gets caught for both hosts at once.

`_inject_graph_ui()` ships the module into the page, guarded by a one-line probe, because
`_DRIVING_UI_JS` re-runs on *every* attached read and re-sending it per click would stall
each action.

**Editing `graph_ui.js` does not affect a running MCP server.** `_GRAPH_UI_JS` is built at
*import* time, so a live `zerodom-mcp` keeps serving the copy it started with — a page
reload won't help, and the symptom is a sidebar that silently lacks whatever you just
added. Restart the MCP server. The extension popup has the opposite behaviour: it reads
`graph_ui.js` off disk on each popup open, so reloading the unpacked extension is enough.

## Conventions specific to this codebase

- Every non-obvious selector/label decision has a comment explaining *why*, usually
  anchored to a concrete real-world page (Hacker News numeric ids, browser-injected
  `<tbody>`, etc.) — when changing selector or label logic, check whether the existing
  comment's reasoning still holds before removing it, and check `README.md`'s "Design
  notes" section, which documents the same set of edge cases from the user's side.
  `tests/test_parser.py` has one test per such edge case (e.g.
  `test_table_rows_get_the_tbody_browsers_inject`,
  `test_numeric_and_odd_ids_use_attribute_selectors`) — if you touch
  `_selector`/`_path`/`css_id`/`class_token`, run that file and check whether a new case
  needs the same treatment.
- Comments prefixed `# ponytail:` mark a deliberate scope limit, not a bug — see
  `mcp_server.py`'s single-session comment for the pattern. Don't "fix" these without
  checking whether the limitation is intentional.
- No LLM/network calls belong in `parser.py` or `label_linker.py` — the parse must stay
  deterministic and dependency-free of a browser. Browser-only code lives in
  `playwright_wrapper.py`, `report.py`, `mcp_server.py`, or the `--render`/`capture` path
  of `cli.py`.
- `__init__.py`'s version comes from `importlib.metadata.version("zerodom")`, reading
  `pyproject.toml` — never hardcode a version string elsewhere in the Python package.
  `js/package.json`, `server.json` and `mcpb/manifest.json` carry their own copies and are
  bumped by hand.
- **Three hand-kept mirrors, no shared source of truth.** A fix isn't done until every
  copy has it: (1) `parser.py`/`label_linker.py` → `js/src/parser.ts`/`labelLinker.ts` plus
  a case in `js/test/parser.test.ts`; (2) browser-side stamping in `SERIALIZE`
  (`playwright_wrapper.py`) → the `data-zerodom-*` reader in `parser.py` *and*
  `js/src/parser.ts`; (3) the sensitive-field regex, in `_SENSITIVE_FIELD_RE`
  (the server-side fill refusal) and `SENSITIVE_RE` in `graph_ui.js` — the human is
  meant to see exactly the fields the model is refused on.
  The graph UI used to be a fourth; it is now one shared file, guarded by a test —
  see above. Prefer that shape when one of these three next needs real work.
- `docs/` is gitignored (decision log, strategy, prospect names) — `docs/DECISIONS.md`
  numbers every non-obvious product decision (D1–D29+) and is the place to look locally for
  "why is it like this", but never assume a fresh clone has it, and don't quote it into
  published files.
