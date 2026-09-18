# Roadmap: Chrome- and Firefox-driven browsing

Letting Claude drive the user's real, already-logged-in browser — real cookies,
no bot-detection wall — through zerodom's existing compact, verified-unique-selector
graph, instead of a screenshot or a raw accessibility-tree dump. Background and the
architecture decisions: `docs/DECISIONS.md` D10, D11, D12.

**D12 partially reverses D11** (2026-09-19): D11's relay (Stage 3) stays exactly
as built — it never cared which extension was the client. What changes is the
Chrome *client*: our own unpacked extension instead of Microsoft's, because
depending on theirs is a hard ceiling on ever adding zerodom-specific capability
into the extension itself (a real side panel, our own permission UI), not just
a branding cost. Firefox/Zen support is now real, committed scope — structurally
different from Chrome, scoped from actual research, not assumption.

Status legend: `todo` · `in progress` · `done` · `blocked` · `superseded`

## Chrome

| Stage | What | Proves | Status |
|---|---|---|---|
| 0 | `ZERODOM_CDP_ENDPOINT` branch in `mcp_server.py`'s `_page()`, plus the `_restart()` close-vs-disconnect fix. | The rest of the pipeline (`_diff`, `frames.py`, `playwright_wrapper.py`) works unmodified over `connect_over_cdp`; a crash never closes a browser we only attached to. Covered by `tests/test_mcp_cdp.py` (4 tests, full suite green). | done — still the foundation everything else connects through |
| ~~1~~ | ~~Custom MV3 extension, first attempt~~ | `chrome.debugger` genuinely gets past the Chrome 136 default-profile CDP lockout — empirically proven. That finding stays true and load-bearing into Stage 6 below. | superseded by D11, revived by D12 |
| ~~2~~ | ~~Native-messaging host + `zerodom native-install`~~ | Real, working native-messaging round-trip — the mechanism worked, it just isn't what any extension here actually needs (D12: WebSocket-direct, not native messaging, either way). | superseded by D11, stays superseded |
| 3 | The Python relay server (`zerodom/relay.py`): `BrowserModel` + `handle_cdp_command` + `RelayServer` — a real `websockets` server, two endpoints (`/cdp/*` for our own `connect_over_cdp`, `/extension/*` for whichever extension is the client). | 25 unit tests against fake extension calls, 5 more running genuine WebSocket connections end-to-end — including a real bug caught and fixed (event/response send ordering via one ordered outgoing queue). Full suite: 165 passed. **Unaffected by D12** — this is the part that was always going to be ours. | done |
| 4 | `zerodom relay` CLI command — starts the server, prints what to open and what to export. | Ran for real: starts, listens, prints a correct connect URL and endpoint. Printed instructions updated in Stage 6 to point at our own extension's popup instead of Microsoft's connect-page flow. | done |
| 5a | Highlight pulse (`_highlight()`) injected via `locator.evaluate()` before a click/fill — amber outline, ~350ms, auto-removed. Gated to attached sessions, cosmetic-only, never blocks the real action. | 3 tests: fires attached, skipped headless, survives injection failure. Full suite: 168 passed. | done |
| 5b | On-page activity log (`_log_action()`/`_LOG_JS`) — floating transcript, one line per action, capped at 6 lines. Same injection mechanism and guarantees as 5a. | 4 tests: click, fill-with-value, skipped headless, survives injection failure. Full suite: 172 passed. | done |
| **6** | **Our own Chrome extension** (`extension/`: `manifest.json`, `background.js`, `popup.html`/`popup.js`), speaking the same relay protocol Stage 3 already implements (`chrome.debugger.attach/detach/sendCommand`, `chrome.tabs.create/remove`, the matching events) — revives Stage 1's proven `chrome.debugger` mechanism, now as the real, permanent client instead of a superseded first draft. Connects directly to the relay's `/extension/*` WebSocket (no native messaging, no Microsoft connect-page indirection needed since we control both ends). Ships unpacked (Developer Mode) — not published to the Chrome Web Store yet. | Real end-to-end run, not just unit tests: real relay + real `--load-extension` Chromium + the actual `background.js` connecting, `connect_over_cdp` reading the real page title through it, and a real `Locator.click()` navigating a real page (`example.com` → `iana.org/help/example-domains`) through the real `chrome.debugger.sendCommand` relay. | done |
| 6b | **Live verification against the user's real, daily Chrome** — not scripted. Four relay/extension bugs found and fixed (service-worker eviction, a stale `_extension_ready` latch, orphaned `chrome.debugger` attachments, and the relay's 1 MiB WebSocket frame cap), plus the actual root cause of most of the apparent flakiness: a second CDP connection never got told about tabs a first connection had already attached. Fixed relay endpoint (`ws://127.0.0.1:8765`, fixed `id="local"`) so it never needs re-wiring after a restart. Full findings and fixes: `docs/DECISIONS.md` D13. | `mcp__zerodom__*` tools driving a real, already-open YouTube tab repeatedly and reliably — read, fill, click, scroll — no scripts, no manual reconnects, same interface shape as claude-in-chrome. `tests/test_relay.py`'s `test_a_second_auto_attach_reannounces_already_attached_tabs` covers the root-cause fix. Full suite: 175 passed. | done |
| 7 | Chrome Web Store publish, for real, with our own listing/review. | Zero-friction install for anyone, not just Developer-Mode users. | todo — after Stage 6 proves itself, no deadline pressure |

**Two hard limits, confirmed during 6b, not zerodom gaps:**
- Real DevTools can't be open on a tab at the same time as `chrome.debugger` — Chrome allows only one Inspector-protocol client per target, full stop.
- Real browser-window resize isn't exposed through a per-tab `chrome.debugger` session — that needs browser-level window management chrome.debugger doesn't grant. (`Emulation.setDeviceMetricsOverride` can resize the page's *viewport* instead — not built, see below.)

## Firefox / Zen

Zen is Firefox-based, so this line of work covers both. Structurally different
from Chrome — see D12 for the full research findings this is scoped from.

| Stage | What | Proves | Status |
|---|---|---|---|
| F0 | Research pass on the actual W3C WebDriver BiDi spec (not a summary) — the minimal command surface zerodom needs: navigate, evaluate, element rects, click/type dispatch. | A concrete, accurate build plan, same rigor as the Chrome relay got before it was built. | todo — next real step for Firefox |
| F1 | A minimal Python BiDi client against a **throwaway** Firefox/Zen profile — proves the protocol client works at all before it's trusted with anything real. No Playwright `connect_over_bidi` to lean on (confirmed absent from the installed version) — this is written from scratch. | Navigate + evaluate + click round-trip through real BiDi, on disposable state. | todo |
| F2 | The safety guard, non-optional: `user_pref("remote.prefs.recommended", false)` written before *every* launch, real profile or not — this is what actually prevents the ~108-recommended-prefs permanent-corruption risk, not which profile is used. | A test that a launch without the guard applied is refused, not just discouraged. | todo |
| F3 | Real-profile use, once F1+F2 are solid. Quit-and-relaunch UX (no live-attach exists for Firefox, confirmed) — the launcher handles the "please quit Firefox/Zen first" flow explicitly rather than failing unhelpfully. | The actual target experience: real logins, real cookies, safely. | todo |
| F4 | zerodom's parser/diff/highlight/log pipeline wired onto the BiDi client, mirroring Stages 5a/5b on the Chrome side. | Feature parity with Chrome, modulo the two structural differences below. | todo |

**Two honest, permanent differences from Chrome, not bugs to fix:**
- No live-attach — every Firefox/Zen session starts with quitting and relaunching, never mid-session attach the way Chrome's extension allows.
- `navigator.webdriver = true` while the BiDi agent is active (Mozilla bug #1719505) — bot-detection checking that flag still sees the session as automated, unlike the Chrome path.

## Deliberately not built (yet)

Recorded so these don't get rebuilt on a whim — see `docs/DECISIONS.md` for the
project's general convention on this:

| Not built | Why | Revisit when |
|---|---|---|
| Chrome Web Store publish of our own extension | Stage 6 needs to exist and prove itself first; a new `chrome.debugger` listing's review timeline is unpredictable and not worth racing | Stage 6 is solid and there's no deadline pressure |
| Native messaging as any bridge, Chrome or Firefox | A real WebSocket relay (Chrome) / BiDi client (Firefox) is simpler and doesn't need OS-level manifest registration | Never, barring a reason not visible now |
| Multi-tab/multi-session attach, or any tab-selection policy | Matches the existing single-global-session scope limit in `mcp_server.py`; `_page()` still just grabs `pages[0]`, confirmed unreliable with >1 tab attached (D13) | Concurrent sessions or multi-tab profiles are a real, demonstrated need |
| Hover, keyboard-only actions (Enter/Escape/Tab without filling), drag-and-drop, file upload | Not requested until the 6b live-verification pass surfaced them as real gaps against a real site (D13) | Any one is actually blocking a real task |
| Screenshot of the *live attached* session as an MCP tool | `report.py`'s screenshot path exists but is wired to the CLI's own throwaway browser, not the attached `mcp_server.py` session | The same visual-proof need that motivated 5a/5b's highlight+log comes up for screenshots specifically |
| Viewport resize via `Emulation.setDeviceMetricsOverride` | Real window resize is impossible (see the two hard limits above); viewport-only resize wasn't requested for its own sake, only floated as a partial substitute | Testing responsive layouts through zerodom becomes a real need |
| Persistent "always allow" permission storage | A half-built version (unsigned, forgeable) is exactly what broke in Anthropic's own shipped Claude-in-Chrome extension | A tamper-evident (OS-keychain or signed) implementation is actually built, not before |
| Rate limiting / confirmation-fatigue mitigation | Nothing beyond a fixed hard-block list is justified yet | A real incident points at a specific gap |
| A bespoke RPC instead of the real relay protocol (Chrome) | Would fork the duck-typing contract `playwright_wrapper.py`/`frames.py`/`test_mcp.py`'s `FakePage` all rely on | Never — speaking the documented protocol is the whole point |
| A Firefox BiDi client before F0's real research pass | Same mistake D10/D11 already made once (building on an assumption instead of the real spec) — not repeating it | F0 is actually done |
