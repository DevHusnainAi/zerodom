"""Model Context Protocol server: drive a browser through the interaction graph."""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server import MCPServer

from . import __version__
from .frames import locate
from .parser import compact_line, find_nodes
from .playwright_wrapper import ZeroDOM

mcp = MCPServer("zerodom", version=__version__)

# ponytail: one global browser *connection* — the MCP server drives a single
# agent — but multiple tabs within it (zerodom_new_tab/switch_tab/close_tab).
# `pages` maps a tab id to its Playwright Page; `active` is the one every
# other tool (_page(), click, fill, read, ...) acts on until switched.
_session: dict[str, Any] = {
    "pw": None, "browser": None, "pages": {}, "active": None, "_next_tab": 0,
    "attached": False, "selectors": {}, "nodes": [], "url": None, "frames": False,
    "network_log": {},  # tab_key -> list of {"type": "request"|"response", ...}
    "cdp_sessions": {},  # tab_key -> CDPSession, cached by _set_input_ignored
    "action_count": 0,  # attached-session actions taken, shown as the bar's Step count
    "aria_cache_url": None,  # sidebar's Playwright-comparison card, cached per URL
    "aria_cache_tokens": None,  # -- see _read(), aria_snapshot() is too expensive to run on every read
}


def _wire_network_log(page: Any, tab_key: str) -> None:
    """Passive request/response visibility for zerodom_network_log — not
    interception or modification (that's page.route(), a bigger, stateful
    feature not built until something actually needs it). Capped at the most
    recent 200 entries per tab so a long session doesn't grow unbounded.
    """
    log: list[dict[str, Any]] = []
    _session["network_log"][tab_key] = log

    def on_request(request: Any) -> None:
        log.append({"type": "request", "method": request.method, "url": request.url})
        del log[:-200]

    def on_response(response: Any) -> None:
        log.append({"type": "response", "status": response.status, "url": response.url})
        del log[:-200]

    page.on("request", on_request)
    page.on("response", on_response)


def _cdp_endpoint() -> str | None:
    """Where to attach instead of launching, or None to launch as usual.

    ZERODOM_CDP_ENDPOINT is the sanctioned power-user/CI path: a Chrome started
    with --remote-debugging-port against a *dedicated* --user-data-dir, never the
    default profile — Chrome has refused CDP on the default profile since v136
    specifically to stop this exact session-theft vector (see docs/DECISIONS.md
    D10). The browser extension's native host (Stage 4) will populate
    ~/.zerodom/bridge.json for the same seam; that discovery path is added then,
    not speculated here.
    """
    return os.environ.get("ZERODOM_CDP_ENDPOINT") or None


def _relay_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


# Piping the auto-spawned relay's output to DEVNULL made a real bug (a stuck
# extension handshake) take a hand-rolled WebSocket probe script and manual
# process surgery to diagnose. A persistent, append-mode log file costs
# nothing and means `zerodom_status` — or a human with `tail` — can just look.
_RELAY_LOG_PATH = Path.home() / ".zerodom" / "relay.log"


def _ensure_relay_running() -> None:
    """Auto-starts `zerodom relay` in the background so a fresh install needs
    neither a second terminal nor ZERODOM_CDP_ENDPOINT set by hand — the relay
    is ours (D11/D12), so the MCP server can just bring it up itself. A no-op
    if the user already set ZERODOM_CDP_ENDPOINT (explicit config always wins)
    or something's already listening on the fixed port — another zerodom-mcp
    instance, or a manually-run `zerodom relay` — reused rather than duplicated.
    """
    if os.environ.get("ZERODOM_CDP_ENDPOINT"):
        return
    from .relay import DEFAULT_PORT

    host = "127.0.0.1"
    if not _relay_port_open(host, DEFAULT_PORT):
        _RELAY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(_RELAY_LOG_PATH, "a")
        subprocess.Popen(
            [sys.executable, "-m", "zerodom.relay"],
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        for _ in range(25):  # ~5s
            if _relay_port_open(host, DEFAULT_PORT):
                break
            time.sleep(0.2)
    if _relay_port_open(host, DEFAULT_PORT):
        os.environ["ZERODOM_CDP_ENDPOINT"] = f"ws://{host}:{DEFAULT_PORT}/cdp/local"


async def _page() -> Any:
    """The active tab's page, bootstrapping the browser and its first tab
    (id "0") on first call. Every tool goes through this rather than reading
    _session["pages"] directly, so zerodom_switch_tab transparently redirects
    every other tool at whichever tab is now active."""
    active = _session["active"]
    if active is not None and active in _session["pages"]:
        return _session["pages"][active]
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    endpoint = _cdp_endpoint()
    if endpoint:
        # Fail closed: a configured-but-unreachable endpoint is an error, not
        # a reason to silently fall back to a fresh, unauthenticated browser.
        browser = await pw.chromium.connect_over_cdp(endpoint)
        page = browser.contexts[0].pages[0] if browser.contexts[0].pages else await browser.contexts[0].new_page()
        attached = True
    else:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        attached = False
    tab_key = str(_session["_next_tab"])
    _session["_next_tab"] += 1
    _session["pages"][tab_key] = page  # merge, not replace: other tabs may still be open
    _session.update(pw=pw, browser=browser, attached=attached, active=tab_key)
    _wire_network_log(page, tab_key)
    return page


async def _goto(page: Any, url: str) -> None:
    """Navigate, then give a client-hydrated SPA a bounded chance to finish
    mounting its real UI before the first parse.

    domcontentloaded alone isn't enough: it fires on the initial HTML parse,
    before frameworks that hydrate after load have rendered anything real.
    Confirmed on google.com/maps — the first parse (domcontentloaded only)
    found 10 nodes; the exact same unreloaded page, given a few seconds,
    had 39 (every category pill, zoom control, Layers, Menu/Saved/Recents —
    all mounted after domcontentloaded already fired). `networkidle` alone
    is not a safe default here, though — a page with a persistent
    websocket/poll (chat apps, live dashboards) never goes idle and would
    hang the whole call — so this waits for it only as a bounded, best-effort
    top-up, not a requirement: on timeout, proceed with whatever's there.

    Only used for a *fresh* navigation. _read()'s reload-free re-check stays
    on domcontentloaded-only deliberately — a click may still be mid-navigation
    when it runs, and networkidle there risks a much longer, avoidable wait on
    every single action instead of once per navigation.
    """
    await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=2000)
    except Exception:
        pass


def _node(node_id: str) -> dict[str, Any]:
    """Resolve a node id to the node itself — the lookup the model never sees.

    Accepts both the bare index the compact graph prints (`[03]` -> "3") and the
    full "node_03" form used in the JSON graph. Returns the whole node, not just
    its selector, because a node inside an iframe also needs its frame chain to
    be reachable.
    """
    key = node_id if node_id.startswith("node_") else f"node_{node_id.strip('[]').zfill(2)}"
    for node in _session["nodes"]:
        if node["id"] == key:
            return node
    raise ValueError(f"Unknown node '{node_id}'. Call zerodom_parse_url first.")


# Only checked on `fill`, only on attached (real, watched) sessions -- a
# server-side wall, not the agent's own discipline (the compensating control
# examples/mcp_agent_system_prompt.md documented as the *only* thing stopping
# this before now). input_type == "password" is unambiguous; the regex
# catches text-labeled sensitive fields (card number, CVV, SSN, ...) whose
# input type is just "text"/"tel". Not exhaustive -- a determined agent
# ignoring its own system prompt could still work around a label it doesn't
# recognize, but it closes the gap for the common, honest-mistake case.
_SENSITIVE_FIELD_RE = re.compile(
    r"password|passwd|\bpwd\b|card ?number|card ?no\b|\bcvv\b|\bcvc\b|"
    r"security code|\bssn\b|social security|routing number|account number|"
    r"\biban\b|sort code",
    re.IGNORECASE,
)


def _is_sensitive_field(node: dict[str, Any]) -> bool:
    if node.get("input_type") == "password":
        return True
    haystack = f"{node.get('label', '')} {node.get('placeholder', '')}"
    return bool(_SENSITIVE_FIELD_RE.search(haystack))


# Cyan is zerodom's actual brand color — report.py's own BADGE_COLOR is
# #22e0d8, the same used for the "zerodom" tab group and the logo. (An
# earlier version of this comment claimed amber matched report.py's badges;
# it didn't — report.py's badges were always cyan. Corrected here, not
# preserved as-is.)
_HIGHLIGHT_JS = """(el) => {
  const r = el.getBoundingClientRect();
  const box = document.createElement('div');
  box.style.cssText = `position:fixed;left:${r.left}px;top:${r.top}px;` +
    `width:${r.width}px;height:${r.height}px;border:3px solid #22e0d8;` +
    `border-radius:4px;box-shadow:0 0 0 4px rgba(34,224,216,0.35);` +
    `pointer-events:none;z-index:2147483647;transition:opacity .25s ease;`;
  document.body.appendChild(box);
  setTimeout(() => { box.style.opacity = '0'; }, 250);
  setTimeout(() => box.remove(), 600);
}"""

_HIGHLIGHT_PAUSE_S = 0.35


async def _highlight(locator: Any) -> None:
    """A brief pulse over the real element before acting on it — the one thing
    actually visible in an attached session, since chrome.debugger itself never
    opens DevTools or resizes anything (docs/DECISIONS.md D11). Only meaningful
    when a person is watching a real tab, so it's skipped entirely for launched
    (headless, unwatched) sessions — see the `attached` check in `_act()`.

    Cosmetic only: a failure here must never block the real action underneath
    it, so exceptions are swallowed rather than propagated.
    """
    try:
        await locator.evaluate(_HIGHLIGHT_JS)
        await asyncio.sleep(_HIGHLIGHT_PAUSE_S)
    except Exception:
        pass


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


# A small floating transcript, not a growing wall — panel.children[0] is the
# title, so the cap keeps at most 6 log lines plus it. Page-level (page.evaluate,
# not locator.evaluate) because the panel isn't tied to whichever element was
# just acted on, and lives on the top document even when the action happened
# inside a frame — an agent's own activity log belongs with the viewer, not
# buried inside whatever iframe the click happened to be in.
_LOG_JS = """(text) => {
  let panel = document.getElementById('zerodom-log-panel');
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'zerodom-log-panel';
    panel.setAttribute('aria-hidden', 'true');
    panel.style.cssText = 'position:fixed;bottom:16px;right:16px;max-width:320px;' +
      'max-height:200px;overflow-y:auto;background:rgba(23,24,27,.92);' +
      'color:#e8eaed;font:12px/1.5 ui-monospace,Menlo,monospace;padding:10px 12px;' +
      'border-radius:8px;border:1px solid rgba(34,224,216,.4);' +
      'box-shadow:0 8px 24px rgba(0,0,0,.35);pointer-events:none;z-index:2147483647;';
    const title = document.createElement('div');
    title.textContent = 'zerodom';
    title.style.cssText = 'color:#22e0d8;font-weight:600;margin-bottom:4px;';
    panel.appendChild(title);
    document.body.appendChild(panel);
  }
  const line = document.createElement('div');
  line.textContent = text;
  line.style.cssText = 'opacity:0;transition:opacity .2s ease;white-space:nowrap;' +
    'overflow:hidden;text-overflow:ellipsis;';
  panel.appendChild(line);
  requestAnimationFrame(() => { line.style.opacity = '1'; });
  while (panel.children.length > 7) panel.removeChild(panel.children[1]);
}"""


# Updates any subset of the bar's live segments (#zerodom-bar-<key>) by id —
# a no-op per key until _DRIVING_UI_JS has actually built the bar (first
# action of the session), same fail-quiet shape as everything else here.
_BAR_SEGMENTS_JS = """(seg) => {
  for (const key in seg) {
    const el = document.getElementById('zerodom-bar-' + key);
    if (el) el.textContent = seg[key];
  }
}"""

# Refreshes the graph sidebar's data (see _DRIVING_UI_JS's sidebar block,
# which defines window.__zerodomUpdateGraph) -- a no-op until the sidebar
# exists, same fail-quiet shape as everything else here.
_SIDEBAR_DATA_JS = """(nodes) => {
  if (window.__zerodomUpdateGraph) window.__zerodomUpdateGraph(nodes);
}"""

_SIDEBAR_STATS_JS = """(data) => {
  const el = document.getElementById('zerodom-sidebar-stats');
  if (!el) return;
  el.innerHTML =
    '<div style="font-size:10px;color:#7dd6d0;letter-spacing:.03em;">' +
      'vs playwright\\'s own aria snapshot</div>' +
    '<div style="font-size:22px;font-weight:700;color:#22e0d8;line-height:1.35;">' +
      data.pct + '</div>' +
    '<div style="font-size:11px;color:#9199a3;">' + data.detail + '</div>';
}"""


async def _log_action(page: Any, text: str) -> None:
    """Append one line to the on-page activity panel — the audit-trail piece
    Stage 5b exists for (ROADMAP.md) — and mirror it into the bar's Target
    segment plus bump the Step counter, so the bar shows live session info
    rather than a fixed "is driving this tab" string. Same reasoning as
    `_highlight`: gated to attached sessions in `_act()`, cosmetic only,
    never allowed to block the real action it's describing.
    """
    _session["action_count"] += 1
    try:
        await page.evaluate(_LOG_JS, text)
        await page.evaluate(_BAR_SEGMENTS_JS, {
            "target": text,
            "step": f"step {_session['action_count']}",
        })
    except Exception:
        pass


# Real zerodom logo (assets/logo.svg), inlined as literal <svg> markup, not
# an <img src="data:..."> -- some real sites (confirmed live on iana.org)
# set a strict img-src CSP with no `data:` scheme, which silently blocks a
# data-URI <img> even though the extension injected it. CSP's img-src only
# governs fetched image *resources*; inline SVG markup is just DOM content,
# so it renders regardless of the page's own CSP.
_LOGO_SVG_INLINE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="14" height="14" '
    'style="display:block;flex:none;border-radius:3px;">'
    '<rect width="64" height="64" rx="14" fill="#080b16"/>'
    '<g fill="#3e5098"><circle cx="14" cy="14" r="2.6"/>'
    '<circle cx="25" cy="14" r="2.6" opacity=".8"/><circle cx="36" cy="14" r="2.6" opacity=".6"/>'
    '<circle cx="14" cy="25" r="2.6" opacity=".8"/><circle cx="25" cy="25" r="2.6" opacity=".6"/>'
    '<circle cx="14" cy="36" r="2.6" opacity=".6"/></g>'
    '<g fill="none" stroke="#22e0d8" stroke-width="3" stroke-linecap="round">'
    '<path d="M31 43 L47 33 M31 43 L47 52"/></g>'
    '<g fill="#22e0d8"><circle cx="31" cy="43" r="5"/><circle cx="48" cy="32" r="4"/>'
    '<circle cx="48" cy="53" r="4"/></g></svg>'
)

# Purely cosmetic now — the real user-input block is _set_input_ignored()
# below, at the browser/CDP level, not here. A page-injected pointer-events
# overlay can't tell a real click apart from a chrome.debugger-dispatched
# one (both are trusted DOM events), so it can only ever swallow both or
# neither; Input.setIgnoreInputEvents is what actually distinguishes them.
# This draws a thin action bar with real typographic hierarchy instead of a
# pipe-divided debug line: sans-serif for the one thing worth actually
# reading (Target, the live action), a single quiet monospace "stat chip"
# grouping Path/Tokens/Step (data, not prose), cyan pulled back to just the
# logo/ACTIVE-dot/CTA rather than smeared across every divider. Segments are
# updated live and independently by id (#zerodom-bar-target/path/tokens/step)
# via _BAR_SEGMENTS_JS -- see _log_action (Target, Step) and _read() (Path,
# Tokens). Deliberately no "Step N/total": the server has no way to know an
# agent's planned total step count, so it only shows a real, counted-so-far
# number, never a guessed one. Structure is built once, idempotent.
# Everything stays pointer-events:none except the waitlist link, a normal
# fixed-destination anchor — it must never intercept a real click elsewhere.
_DRIVING_UI_JS = """() => {
  if (document.getElementById('zerodom-driving-border')) return;
  const BAR_HEIGHT = 36;
  document.body.style.marginTop = BAR_HEIGHT + 'px';

  // body's margin-top only pushes normal document flow -- a page's own
  // `position: fixed`/`sticky` navbar is viewport-anchored and ignores it
  // entirely, so it renders right under our bar (confirmed live: Reddit's
  // own header). Cheap, targeted fix: find whatever the page actually
  // renders at a point just below our bar (not a full-DOM scan), walk up
  // to the nearest fixed/sticky ancestor pinned at the viewport top, and
  // nudge only that one element's `top` down by BAR_HEIGHT -- for a sticky
  // element this makes it stick just below our bar instead of at 0, which
  // is the correct behavior, not a side effect.
  //
  // Reddit's own header lives inside a custom element's shadow root
  // (<reddit-header-large>, open shadow DOM) -- plain elementFromPoint()
  // and .parentElement both stop at the shadow boundary and return the
  // host, which is itself just a static wrapper, so the real fixed div
  // inside never got found. zerodomDeepPoint/zerodomParent pierce shadow
  // roots on the way down and back up.
  function zerodomDeepPoint(x, y) {
    let el = document.elementFromPoint(x, y);
    while (el && el.shadowRoot) {
      const inner = el.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  }
  function zerodomParent(node) {
    if (node.parentElement) return node.parentElement;
    const root = node.getRootNode();
    return root instanceof ShadowRoot ? root.host : null;
  }
  function zerodomPushFixedHeader() {
    let node = zerodomDeepPoint(Math.floor(window.innerWidth / 2), BAR_HEIGHT + 4);
    let depth = 0;
    while (node && node !== document.documentElement && depth < 60) {
      const cs = getComputedStyle(node);
      if (cs.position === 'fixed' || cs.position === 'sticky') {
        const rect = node.getBoundingClientRect();
        if (rect.top <= 4 && rect.top >= -1 && !node.dataset.zerodomPushed) {
          node.dataset.zerodomPushed = '1';
          node.style.top = (parseFloat(cs.top) || 0) + BAR_HEIGHT + 'px';
        }
        break;
      }
      node = zerodomParent(node);
      depth++;
    }
  }
  zerodomPushFixedHeader();

  // Inline styles can't set ::-webkit-scrollbar (it's a pseudo-element, not
  // a property) -- a real <style> tag is the only way. Scoped by id so it
  // never touches the page's own scrollbars.
  const style = document.createElement('style');
  style.textContent =
    '#zerodom-sidebar-list, #zerodom-log-panel { scrollbar-width: thin; ' +
      'scrollbar-color: rgba(34,224,216,.35) transparent; }' +
    '#zerodom-sidebar-list::-webkit-scrollbar, #zerodom-log-panel::-webkit-scrollbar ' +
      '{ width: 8px; }' +
    '#zerodom-sidebar-list::-webkit-scrollbar-track, ' +
      '#zerodom-log-panel::-webkit-scrollbar-track { background: transparent; }' +
    '#zerodom-sidebar-list::-webkit-scrollbar-thumb, ' +
      '#zerodom-log-panel::-webkit-scrollbar-thumb ' +
      '{ background: rgba(34,224,216,.3); border-radius: 4px; }' +
    '#zerodom-sidebar-list::-webkit-scrollbar-thumb:hover, ' +
      '#zerodom-log-panel::-webkit-scrollbar-thumb:hover ' +
      '{ background: rgba(34,224,216,.5); }';
  document.head.appendChild(style);

  const banner = document.createElement('div');
  banner.id = 'zerodom-driving-bar';
  // aria-hidden -- parser.py's _is_hidden() prunes this whole subtree, so
  // the bar's own "Join Waitlist" <a> never shows up as an actionable node
  // in the graph an agent is driving from.
  banner.setAttribute('aria-hidden', 'true');
  banner.style.cssText = 'position:fixed;top:0;left:0;right:0;height:' + BAR_HEIGHT + 'px;' +
    'display:flex;align-items:center;padding:0 12px;box-sizing:border-box;' +
    'background:#0a0b0d;border-bottom:1px solid rgba(255,255,255,.08);' +
    'font:12px -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;' +
    'color:#e8eaed;pointer-events:none;z-index:2147483646;';
  const MONO = 'font-family:ui-monospace,"SF Mono",Menlo,monospace;';
  banner.innerHTML =
    '<span style="display:flex;align-items:center;gap:6px;flex:none;margin:6px 0;' +
      'padding:3px 10px;border:1px solid rgba(255,255,255,.14);border-radius:6px;">' +
      '__LOGO_SVG__' +
      '<strong style="color:#e8eaed;font-weight:600;">zerodom</strong>' +
    '</span>' +
    '<span id="zerodom-bar-active" style="display:flex;align-items:center;gap:5px;' +
      'flex:none;margin-left:10px;font-size:10px;font-weight:700;letter-spacing:.06em;' +
      'color:#7d828c;">' +
      '<span style="width:6px;height:6px;border-radius:50%;background:#4fbf7f;' +
        'box-shadow:0 0 5px #4fbf7f;flex:none;"></span>ACTIVE</span>' +
    '<span id="zerodom-bar-target" style="flex:1;min-width:0;margin-left:14px;' +
      'color:#e8eaed;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' +
      'watching</span>' +
    '<span style="flex:none;display:flex;align-items:center;gap:7px;margin-left:12px;' +
      'padding:4px 10px;border-radius:6px;background:rgba(255,255,255,.05);' +
      MONO + 'font-size:11px;color:#9199a3;">' +
      '<span style="color:#5b5f68;">path</span>' +
      '<span id="zerodom-bar-path" style="max-width:130px;overflow:hidden;' +
        'text-overflow:ellipsis;white-space:nowrap;"></span>' +
      '<span style="color:#3d4048;">&middot;</span>' +
      '<span id="zerodom-bar-tokens"></span>' +
      '<span style="color:#3d4048;">&middot;</span>' +
      '<span id="zerodom-bar-step">step 0</span>' +
    '</span>' +
    '<a href="https://zerodom.vexralabs.com/#waitlist" target="_blank" rel="noopener" ' +
      'style="pointer-events:auto;flex:none;margin:6px 0 6px 14px;padding:4px 12px;' +
      'border-radius:6px;background:#22e0d8;color:#0a0b0d;font-weight:600;font-size:12px;' +
      'text-decoration:none;white-space:nowrap;">Join Waitlist</a>';
  document.body.appendChild(banner);

  // ---- Graph sidebar: browse every node zerodom currently sees, click one
  // to live-check its selector against the real DOM right now (reuses
  // audit.py's ok/dead/ambiguous classification, just triggered by a human
  // click here instead of a CLI run). Data comes from _SIDEBAR_DATA_JS,
  // called by _read() on every attached read -- window.__zerodomUpdateGraph
  // is how that data lands here. Hidden by default; opens only on the
  // toggle button's own click, so it never costs anything unopened.
  const sidebarBtn = document.createElement('button');
  sidebarBtn.id = 'zerodom-sidebar-toggle';
  sidebarBtn.type = 'button';
  sidebarBtn.textContent = '▤';
  sidebarBtn.style.cssText = 'pointer-events:auto;flex:none;margin:6px 0 6px 10px;' +
    'width:26px;height:24px;border-radius:6px;border:1px solid rgba(255,255,255,.14);' +
    'background:transparent;color:#9199a3;font-size:13px;line-height:1;cursor:pointer;';
  banner.insertBefore(sidebarBtn, banner.lastElementChild);

  const SIDEBAR_WIDTH = 320;
  const sidebar = document.createElement('div');
  sidebar.id = 'zerodom-sidebar';
  sidebar.style.cssText = 'position:fixed;top:' + BAR_HEIGHT + 'px;right:0;bottom:0;' +
    'width:' + SIDEBAR_WIDTH + 'px;background:#0a0b0d;border-left:1px solid rgba(255,255,255,.1);' +
    'font:12px -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;color:#e8eaed;' +
    'z-index:2147483645;display:none;flex-direction:column;';
  sidebar.innerHTML =
    '<div style="display:flex;align-items:center;justify-content:space-between;' +
      'padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.08);flex:none;">' +
      '<strong id="zerodom-sidebar-count" style="font-size:12px;font-weight:600;">graph</strong>' +
      '<button id="zerodom-sidebar-close" type="button" style="pointer-events:auto;' +
        'background:none;border:none;color:#9199a3;font-size:16px;line-height:1;cursor:pointer;' +
        'padding:2px;">&times;</button>' +
    '</div>' +
    '<div id="zerodom-sidebar-stats" style="margin:12px 14px 0;padding:10px 12px;' +
      'border-radius:8px;background:rgba(34,224,216,.08);border:1px solid rgba(34,224,216,.2);' +
      MONO + '"></div>' +
    '<div style="padding:10px 14px 0;flex:none;">' +
      '<input id="zerodom-sidebar-search" type="text" placeholder="filter by label" ' +
        'style="pointer-events:auto;width:100%;box-sizing:border-box;' +
        'background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.14);' +
        'border-radius:6px;padding:6px 9px;color:#e8eaed;outline:none;font-size:12px;' + MONO + '" />' +
      '<div id="zerodom-sidebar-chips" style="display:flex;flex-wrap:wrap;gap:6px;margin:10px 0;"></div>' +
    '</div>' +
    '<div id="zerodom-sidebar-list" style="flex:1;overflow-y:auto;padding:2px 10px 10px;' +
      MONO + '"></div>';
  document.body.appendChild(sidebar);

  function zerodomOpenSidebar(open) {
    sidebar.style.display = open ? 'flex' : 'none';
    sidebarBtn.style.color = open ? '#22e0d8' : '#9199a3';
    sidebarBtn.style.borderColor = open ? '#22e0d8' : 'rgba(255,255,255,.14)';
    // Reserves real space like Chrome's own side panel (docked, page
    // resizes) rather than floating a shadowed overlay on top of content.
    document.body.style.marginRight = open ? SIDEBAR_WIDTH + 'px' : '';
  }
  sidebarBtn.addEventListener('click', () => zerodomOpenSidebar(sidebar.style.display === 'none'));
  sidebar.querySelector('#zerodom-sidebar-close').addEventListener('click', () => zerodomOpenSidebar(false));

  function zerodomRenderChips() {
    const counts = {};
    for (const n of window.__zerodomGraph) counts[n.type] = (counts[n.type] || 0) + 1;
    const chips = document.getElementById('zerodom-sidebar-chips');
    const active = chips.dataset.active || 'all';
    const types = ['all'].concat(Object.keys(counts).sort());
    chips.innerHTML = types.map((t) => {
      const n = t === 'all' ? window.__zerodomGraph.length : counts[t];
      const on = t === active;
      return '<button type="button" data-type="' + t + '" style="pointer-events:auto;' +
        'padding:3px 8px;border-radius:12px;font-size:11px;cursor:pointer;' +
        'border:1px solid ' + (on ? '#22e0d8' : 'rgba(255,255,255,.14)') + ';' +
        'background:' + (on ? 'rgba(34,224,216,.12)' : 'transparent') + ';' +
        'color:' + (on ? '#22e0d8' : '#9199a3') + ';">' + t + ' ' + n + '</button>';
    }).join('');
    chips.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
      chips.dataset.active = b.dataset.type;
      zerodomRenderChips();
      window.__zerodomRenderList();
    }));
  }

  window.__zerodomGraph = window.__zerodomGraph || [];

  window.__zerodomRenderList = function () {
    const list = document.getElementById('zerodom-sidebar-list');
    const countEl = document.getElementById('zerodom-sidebar-count');
    const search = document.getElementById('zerodom-sidebar-search');
    const chips = document.getElementById('zerodom-sidebar-chips');
    const q = ((search && search.value) || '').toLowerCase();
    const activeType = (chips && chips.dataset.active) || 'all';
    const rows = window.__zerodomGraph.filter((n) =>
      (activeType === 'all' || n.type === activeType) &&
      (!q || (n.label || '').toLowerCase().includes(q))
    );
    countEl.textContent = 'graph · ' + rows.length + '/' + window.__zerodomGraph.length;
    list.innerHTML = '';
    for (const n of rows) {
      const row = document.createElement('div');
      row.style.cssText = 'pointer-events:auto;display:flex;align-items:center;' +
        'justify-content:space-between;gap:8px;padding:5px 6px;border-radius:5px;cursor:pointer;';
      const left = document.createElement('span');
      left.style.cssText = 'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;';
      const idSpan = document.createElement('span');
      idSpan.style.color = '#22e0d8';
      idSpan.textContent = '[' + n.id + '] ';
      const typeSpan = document.createElement('span');
      typeSpan.style.color = '#9199a3';
      typeSpan.textContent = n.type + ' ';
      const labelSpan = document.createElement('span');
      labelSpan.textContent = JSON.stringify(n.label || '');
      left.append(idSpan, typeSpan, labelSpan);
      const badge = document.createElement('span');
      badge.style.cssText = 'flex:none;font-size:10px;';
      badge.style.color = '#5b5f68';
      row.append(left, badge);
      row.addEventListener('mouseenter', () => { row.style.background = 'rgba(255,255,255,.05)'; });
      row.addEventListener('mouseleave', () => { row.style.background = 'transparent'; });
      row.addEventListener('click', () => {
        let count;
        try { count = document.querySelectorAll(n.selector).length; } catch (e) { count = -1; }
        const verdict = count < 0 ? 'invalid' : count === 0 ? 'dead' : count === 1 ? 'ok' : 'ambiguous';
        const color = { ok: '#4fbf7f', dead: '#e05252', ambiguous: '#e0a83d', invalid: '#e05252' };
        badge.textContent = verdict + (count > 1 ? ' (' + count + ')' : '');
        badge.style.color = color[verdict];
      });
      list.appendChild(row);
    }
  };

  window.__zerodomUpdateGraph = function (nodes) {
    window.__zerodomGraph = nodes;
    zerodomRenderChips();
    window.__zerodomRenderList();
  };

  sidebar.querySelector('#zerodom-sidebar-search').addEventListener('input', () => {
    zerodomRenderChips();
    window.__zerodomRenderList();
  });

  // Path is pure URL state -- unlike Target/Tokens/Step (which genuinely
  // describe zerodom's own last action and should only change when it acts
  // again), Path can and should track the real address bar even when a
  // client-side-routed SPA (confirmed live on Reddit) or the user's own
  // click navigates without zerodom ever re-reading the page.
  const updateBarPath = () => {
    const el = document.getElementById('zerodom-bar-path');
    if (el) el.textContent = location.pathname || '/';
    zerodomPushFixedHeader();
  };
  updateBarPath();
  const origPushState = history.pushState;
  history.pushState = function (...args) {
    origPushState.apply(this, args);
    updateBarPath();
  };
  const origReplaceState = history.replaceState;
  history.replaceState = function (...args) {
    origReplaceState.apply(this, args);
    updateBarPath();
  };
  window.addEventListener('popstate', updateBarPath);

  // pushState fires synchronously, often *before* the SPA's router has
  // actually re-rendered the new header component (confirmed live on
  // Reddit: the header is a custom element the router recreates, not
  // reuses, on a route change -- and does so more than once mid-transition,
  // so even a couple of fixed-delay retries after pushState still missed
  // it). A MutationObserver reacting to the real DOM change, debounced so
  // it only actually runs the check once mutations settle for a moment,
  // handles both an arbitrarily-delayed and a multi-step re-render without
  // guessing a timeout. zerodomPushFixedHeader() is cheap and idempotent
  // (the dataset.zerodomPushed guard) even when re-run on every settle.
  let pushDebounce = null;
  new MutationObserver(() => {
    clearTimeout(pushDebounce);
    pushDebounce = setTimeout(zerodomPushFixedHeader, 120);
  }).observe(document.body, { childList: true, subtree: true });

  const border = document.createElement('div');
  border.id = 'zerodom-driving-border';
  border.setAttribute('aria-hidden', 'true');
  // Flush with the viewport edges (no inset) -- a pure shadow, no border
  // line, so it reads as a soft glow rather than a drawn rectangle.
  border.style.cssText = 'position:fixed;top:' + BAR_HEIGHT + 'px;right:0;bottom:0;left:0;' +
    'z-index:2147483646;box-shadow:inset 0 0 22px rgba(34,224,216,0.16);' +
    'pointer-events:none;';
  document.body.appendChild(border);

  const cursor = document.createElement('div');
  cursor.id = 'zerodom-cursor';
  cursor.setAttribute('aria-hidden', 'true');
  cursor.style.cssText = 'position:fixed;left:-100px;top:-100px;width:0;height:0;' +
    'border-left:10px solid transparent;border-right:10px solid transparent;' +
    'border-top:18px solid #22e0d8;' +
    'filter:drop-shadow(0 0 1px rgba(0,0,0,.9)) drop-shadow(0 0 3px rgba(0,0,0,.8))' +
    ' drop-shadow(0 2px 4px rgba(0,0,0,.6));' +
    'transition:left .15s ease,top .15s ease;pointer-events:none;z-index:2147483647;';
  document.body.appendChild(cursor);
}"""
_DRIVING_UI_JS = _DRIVING_UI_JS.replace("__LOGO_SVG__", _LOGO_SVG_INLINE)

_MOVE_CURSOR_JS = """(el) => {
  const cursor = document.getElementById('zerodom-cursor');
  if (!cursor) return;
  const r = el.getBoundingClientRect();
  cursor.style.left = (r.left + r.width / 2) + 'px';
  cursor.style.top = (r.top + r.height / 2) + 'px';
}"""


async def _set_input_ignored(page: Any, ignored: bool) -> None:
    """The actual "a real user can't click" block: Chrome's own input
    pipeline (Input.setIgnoreInputEvents over a raw CDP session), not a
    page-injected DOM trick — enforced by the browser before the page's own
    JS or any content it controls ever sees the event. Real, OS-originated
    input is what this flag exists to ignore; chrome.debugger-dispatched
    input (what zerodom's own actions use) is unaffected by it, which is the
    documented purpose of this specific CDP method — but it's still toggled
    around each action in _unlocked_input rather than set once and left on,
    as a defensive measure in case that isn't as absolute as documented.
    Cosmetic-adjacent: a failure here must never block the real action.
    """
    try:
        active = _session["active"]
        cdp = _session["cdp_sessions"].get(active)
        if cdp is None:
            cdp = await page.context.new_cdp_session(page)
            _session["cdp_sessions"][active] = cdp
        await cdp.send("Input.setIgnoreInputEvents", {"ignore": ignored})
    except Exception:
        pass


async def _show_driving(locator: Any) -> None:
    """Shows the driving border/banner (idempotent, stays up), moves the
    visible cursor to the element about to be acted on, ensures real input
    is ignored, then pulses the highlight — the full "an agent is driving
    this" visual, run before every real action on an attached session.
    Cosmetic, so failures never block the real action.
    """
    try:
        page = locator.page
        await page.evaluate(_DRIVING_UI_JS)
        await locator.evaluate(_MOVE_CURSOR_JS)
        await _set_input_ignored(page, True)
    except Exception:
        pass
    await _highlight(locator)


@asynccontextmanager
async def _unlocked_input(page: Any):
    """The one moment real input is allowed through: while zerodom's own
    chrome.debugger-dispatched action is actually running. Real user
    clicks/scroll stay blocked every other moment. A no-op for launched
    (headless, unwatched) sessions — there's no one to block input from.
    """
    if _session["attached"]:
        await _set_input_ignored(page, False)
    try:
        yield
    finally:
        if _session["attached"]:
            await _set_input_ignored(page, True)


async def _act(node_id: str, verb: str, *args: Any) -> str:
    """Click or fill a node, retrying once if the browser drops underneath us.

    Chromium crashed on exactly one of fifty benchmarked sites. A crash kills the
    page but not the session, so a bare retry on a fresh page recovers the run
    instead of ending it.
    """
    node = _node(node_id)
    if verb == "fill" and _session["attached"] and _is_sensitive_field(node):
        raise PermissionError(
            f"Refusing to fill {compact_line(node)} — looks like a "
            "password/payment/SSN field. zerodom won't type into these on "
            "an attached (real, watched) session. If this is a false "
            "positive, ask the user to type it themselves."
        )
    for attempt in (1, 2):
        page = await _page()
        try:
            locator = locate(page, node)
            if _session["attached"]:
                await _show_driving(locator)
            async with _unlocked_input(page):
                await getattr(locator, verb)(*args)
            if _session["attached"]:
                verb_past = {
                    "click": "clicked", "fill": "filled", "hover": "hovered",
                    "press": "pressed", "set_input_files": "uploaded",
                }.get(verb, verb)
                detail = f" = {args[0]!r}" if args else ""
                await _log_action(page, f"{verb_past} {compact_line(node)}{detail}")
            return ""
        except Exception as exc:
            if attempt == 2 or not _is_crash(exc):
                raise
            await _restart(page.url)
            node = _node(node_id)
    return ""


async def _act_drag(source_id: str, target_id: str) -> str:
    """Drag source onto target — same retry-on-crash shape as _act(), but
    _act() can't fit this: it resolves one node into one locator, drag needs
    two. Only the source gets highlighted (that's the element actually moving).
    """
    source_node, target_node = _node(source_id), _node(target_id)
    for attempt in (1, 2):
        page = await _page()
        try:
            source_loc = locate(page, source_node)
            target_loc = locate(page, target_node)
            if _session["attached"]:
                await _show_driving(source_loc)
            async with _unlocked_input(page):
                await source_loc.drag_to(target_loc)
            if _session["attached"]:
                await _log_action(
                    page, f"dragged {compact_line(source_node)} -> {compact_line(target_node)}"
                )
            return ""
        except Exception as exc:
            if attempt == 2 or not _is_crash(exc):
                raise
            await _restart(page.url)
            source_node, target_node = _node(source_id), _node(target_id)
    return ""


def _is_crash(exc: Exception) -> bool:
    text = str(exc).lower()
    return "crash" in text or "target closed" in text or "browser has been closed" in text


async def _restart(url: str) -> None:
    """Rebuild the browser after a crash and return to where we were.

    An attached session (Stage 0+) didn't launch the browser — it's the user's
    real, already-open Chrome. Closing it here would take their browser down on
    every crash-recovery, not just detach from it, so only pw.stop() runs and the
    page/browser handles are dropped without .close() for that case; _page()
    reconnects to the still-live endpoint on the next call.

    Only the crashed (active) tab is torn down — a crash on one tab doesn't
    take down every other tab zerodom_new_tab opened.
    """
    attached = _session.get("attached")
    active = _session.get("active")
    crashed_page = _session["pages"].pop(active, None) if active is not None else None
    if active is not None:
        _session["cdp_sessions"].pop(active, None)
    pw, browser = _session.get("pw"), _session.get("browser")
    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass
    if not attached:
        for obj in (crashed_page, browser):
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
    _session["pw"] = None
    _session["browser"] = None
    _session["attached"] = False
    _session["active"] = None
    page = await _page()
    if url and url != "about:blank":
        await _goto(page, url)
    await _read()


def _identity(node: dict[str, Any]) -> tuple[str, str, str]:
    """What makes a node "the same node" across two reads. Node ids renumber on
    every parse, so they cannot be it."""
    return (node["type"], node["label"], node["selector"])


def _diff(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> str | None:
    """Only what changed, or None when a full graph would be more honest.

    An agent clicking through a page re-reads the same few hundred nodes over and
    over; on Hacker News that is ~2,350 tokens per action to say "one menu opened".
    A wholesale change (a navigation) has no useful diff, so it falls back.
    """
    old, new = {_identity(n): n for n in before}, {_identity(n): n for n in after}
    added = [n for key, n in new.items() if key not in old]
    removed = [n for key, n in old.items() if key not in new]
    changed = [
        (old[key], n)
        for key, n in new.items()
        if key in old and n.get("value") != old[key].get("value")
    ]
    if not (added or removed or changed):
        # Typing sets the DOM *property*, not the `value` attribute, so text a user
        # entered is invisible to any serializer. The fill tool echoes it instead.
        return "no structural change"
    # More than half the page moved: it is a different page, so show all of it.
    if len(added) + len(removed) > len(new) / 2:
        return None

    lines = [f"+{compact_line(n)}" for n in added]
    lines += [f"-{compact_line(n)}" for n in removed]
    lines += [
        f"~{compact_line(n)}  {was.get('value', '')!r} -> {n.get('value', '')!r}"
        for was, n in changed
    ]
    return "\n".join(lines)


async def _read(verbose: bool = False, diff: bool = False) -> str:
    """Parse the live DOM in place and refresh the selector map.

    Never navigates, so form state, scroll position and cookies survive. Every
    action re-reads through here, which is what keeps node ids pointing at the
    page the model is actually looking at.
    """
    page = await _page()
    # A click may still be navigating; content() during that raises.
    await page.wait_for_load_state("domcontentloaded")
    graph = await ZeroDOM.from_page(page, frames=_session["frames"])
    previous, url = _session["nodes"], _session["url"]
    _session.update(
        selectors=graph.selector_map(), nodes=graph["nodes"], url=page.url
    )
    compact = graph.to_compact_text()
    if _session["attached"]:
        # Build the driving bar on the first read (parse_url) so the HUD
        # is visible immediately, not only after the first click/fill.
        # Idempotent — _DRIVING_UI_JS is a no-op if the bar already exists.
        try:
            await page.evaluate(_DRIVING_UI_JS)
        except Exception:
            pass
        # Rough chars/4 estimate, not a tiktoken count (that's benchmark_tokens.py's
        # job, offline) -- good enough for a live "how much smaller is this graph
        # than the raw page" bar readout without spending a real token-counting
        # pass on every read. page.content() here is a second DOM serialization
        # (from_page() already did one internally) -- an extra cost only paid on
        # attached, watched sessions, never on a headless/launched one.
        compact_tokens = max(1, len(compact) // 4)
        try:
            html = await page.content()
            raw_tokens = max(1, len(html) // 4)
            pct = max(0, round((1 - compact_tokens / raw_tokens) * 100))
            tokens_label = _fmt_tokens(compact_tokens) + (f" (-{pct}%)" if pct else "")
            await page.evaluate(_BAR_SEGMENTS_JS, {
                "path": urlsplit(page.url).path or "/",
                "tokens": tokens_label,
            })
        except Exception:
            pass
        try:
            # A real, live comparison against what Playwright MCP itself
            # sends a model -- aria_snapshot(mode="ai") is the exact API
            # Playwright's own browser_snapshot tool calls, not a guess at
            # its output. No equivalent exists for claude-in-chrome: it's a
            # separate, closed extension with no API to reproduce its
            # representation, so it's deliberately not shown here rather
            # than invented.
            #
            # Computing it is genuinely expensive (a full accessibility-tree
            # pass) -- on a page like Reddit's 500+-node feed, doing this on
            # *every* read (every click/hover) made every action noticeably
            # slower for a stat only visible when the sidebar happens to be
            # open. Cached per URL instead: recomputed only on navigation,
            # not on every action taken on the same page. The percentage
            # shown is "as of when this page was last measured," not a
            # byte-perfect live counter -- an acceptable trade for a human
            # debug readout, not something a tool result ever returns.
            if _session["aria_cache_url"] != page.url:
                aria = await page.aria_snapshot(mode="ai")
                _session["aria_cache_url"] = page.url
                _session["aria_cache_tokens"] = max(1, len(aria) // 4)
            aria_tokens = _session["aria_cache_tokens"]
            aria_pct = max(0, round((1 - compact_tokens / aria_tokens) * 100))
            await page.evaluate(_SIDEBAR_STATS_JS, {
                "pct": f"-{aria_pct}%" if aria_pct else "0%",
                "detail": f"{_fmt_tokens(compact_tokens)} tok vs {_fmt_tokens(aria_tokens)} tok",
            })
        except Exception:
            pass
        try:
            # Selectors reaching the page here is not the same boundary as
            # selectors reaching the *model*: this data stays inside
            # page.evaluate()/DOM state for the human-facing sidebar (D15's
            # HUD) and is never returned through a tool result. zerodom_eval_js
            # already lets an agent read arbitrary page state (D14's
            # trust-boundary note), so this isn't a new hole -- and a human
            # driving their own tab already has full devtools access to the
            # same selectors anyway.
            sidebar_nodes = [
                {
                    "id": n["id"].removeprefix("node_"),
                    "type": n["type"],
                    "label": n.get("label", ""),
                    "selector": n["selector"],
                }
                for n in graph["nodes"]
            ]
            await page.evaluate(_SIDEBAR_DATA_JS, sidebar_nodes)
        except Exception:
            pass
    if verbose:
        return graph.to_json()
    # A navigation invalidates every id, so a diff against the old page is noise.
    if diff and previous and url == page.url and (delta := _diff(previous, graph["nodes"])):
        return delta
    return compact


@mcp.tool()
async def zerodom_parse_url(url: str, verbose: bool = False, frames: bool = False) -> str:
    """Navigate to a URL and return its interaction graph.

    Returns the compact text graph: `[03] button 'Sign In'`. CSS selectors are
    kept server-side and resolved by node id, so they never cost context — pass
    verbose=True for the full JSON including selectors.

    Set frames=True when the controls you need are inside an iframe — embedded
    editors, payment fields, consent gates. Off by default because it costs a
    read per frame and most frames on a commercial page are advertising.
    """
    _session["frames"] = frames
    page = await _page()
    await _goto(page, url)
    return await _read(verbose)


@mcp.tool()
async def zerodom_read_page(verbose: bool = False) -> str:
    """Re-read the current page without navigating.

    Use after an action changed the page, or when node ids look stale. Unlike
    zerodom_parse_url this does not reload, so anything typed into the page stays.
    """
    return await _read(verbose)


@mcp.tool()
async def zerodom_find(query: str) -> str:
    """Search the current page's graph for nodes matching `query`.

    Case-insensitive substring match over each node's label and type. Prefer this
    over re-reading the whole page when you already know what you are looking for:
    "checkout" costs three lines, the full graph costs every node on the page.
    """
    hits = find_nodes(_session["nodes"], query)
    if not _session["nodes"]:
        return "No page loaded. Call zerodom_parse_url first."
    if not hits:
        return f"No node matches {query!r} among {len(_session['nodes'])} nodes."
    return "\n".join(compact_line(node) for node in hits)


@mcp.tool()
async def zerodom_click_node(node_id: str) -> str:
    """Click a node and return what changed on the page.

    Returns a diff — `+` appeared, `-` gone, `~` value changed — because most
    clicks alter a handful of nodes and re-listing the page would cost hundreds.
    A navigation renumbers everything, so that returns the full graph instead.
    """
    page = await _page()
    before = page.url
    await _act(node_id, "click")
    page = await _page()
    graph = await _read(diff=True)
    moved = f" -> {page.url}" if page.url != before else ""
    return f"clicked [{node_id}]{moved}\n\n{graph}"


@mcp.tool()
async def zerodom_fill_node(node_id: str, text: str) -> str:
    """Type text into a node and return what changed on the page.

    The text is echoed back in the first line; the diff below it reports *structural*
    change — a validation error appearing, an autocomplete list opening.
    """
    await _act(node_id, "fill", text)
    return f"filled [{node_id}] with {text!r}\n\n{await _read(diff=True)}"


@mcp.tool()
async def zerodom_hover(node_id: str) -> str:
    """Hover over a node and return what changed on the page.

    Reveals hover-triggered menus and tooltips — content that a click alone
    would never surface, and that isn't in the graph until this fires.
    """
    await _act(node_id, "hover")
    return f"hovered [{node_id}]\n\n{await _read(diff=True)}"


@mcp.tool()
async def zerodom_press_key(node_id: str, key: str) -> str:
    """Press a key while a node is focused and return what changed.

    key uses Playwright's key names ("Enter", "Escape", "Tab", "ArrowDown",
    ...). For "press Enter to submit" forms, "Escape to close a modal", and
    keyboard-only widgets a click/fill can't drive.
    """
    await _act(node_id, "press", key)
    return f"pressed {key!r} on [{node_id}]\n\n{await _read(diff=True)}"


@mcp.tool()
async def zerodom_upload_file(node_id: str, path: str) -> str:
    """Set a file input's value to a local file path and return what changed.

    path is resolved on the machine driving the browser — the same trust
    boundary as zerodom_eval_js, not a new one.
    """
    await _act(node_id, "set_input_files", path)
    return f"uploaded {path!r} to [{node_id}]\n\n{await _read(diff=True)}"


@mcp.tool()
async def zerodom_drag(source_node_id: str, target_node_id: str) -> str:
    """Drag source onto target and return what changed.

    Drag-to-reorder lists, drag-and-drop upload zones, sliders — anything a
    click/fill pair can't express because the gesture itself is the input.
    """
    await _act_drag(source_node_id, target_node_id)
    return (
        f"dragged [{source_node_id}] -> [{target_node_id}]\n\n{await _read(diff=True)}"
    )


@mcp.tool()
async def zerodom_scroll(direction: str = "down", amount: int = 800) -> str:
    """Scroll the page and return what's newly visible.

    direction: "up" or "down". amount: pixels, roughly one screenful is 800.
    Dispatches a real wheel event at the viewport center rather than
    `window.scrollBy`, so it scrolls whatever scrollable container is actually
    under the cursor — a nested feed/sidebar, not just the document body,
    matching what a real scroll gesture would do.
    """
    page = await _page()
    delta = amount if direction == "down" else -amount
    async with _unlocked_input(page):
        await page.mouse.wheel(0, delta)
    if _session["attached"]:
        await _log_action(page, f"scrolled {direction} {amount}px")
    return await _read(diff=True)


@mcp.tool()
async def zerodom_new_tab(url: str | None = None) -> str:
    """Open a new tab and make it the active one — every other tool (read,
    click, fill, scroll) then acts on it until you zerodom_switch_tab away.

    In an attached (real-browser) session the new tab lands in the same
    "zerodom" tab group as every other tab this session touches.
    """
    await _page()  # ensure the browser and its first tab exist
    browser = _session["browser"]
    page = await browser.contexts[0].new_page()
    tab_key = str(_session["_next_tab"])
    _session["_next_tab"] += 1
    _session["pages"][tab_key] = page
    _session["active"] = tab_key
    _wire_network_log(page, tab_key)
    if url:
        await _goto(page, url)
    return f"[tab {tab_key}]\n{await _read()}"


@mcp.tool()
async def zerodom_list_tabs() -> str:
    """List every open tab, marking the active one with `*`."""
    await _page()  # ensure at least the first tab exists
    lines = [
        f"{'*' if key == _session['active'] else ' '} [tab {key}] {page.url}"
        for key, page in _session["pages"].items()
    ]
    return "\n".join(lines)


@mcp.tool()
async def zerodom_switch_tab(tab_id: str) -> str:
    """Make another open tab active and return its interaction graph.

    Node ids are per-tab, so re-read (this returns the graph already) before
    clicking or filling anything on the tab you just switched to.
    """
    if tab_id not in _session["pages"]:
        raise ValueError(f"Unknown tab '{tab_id}'. Call zerodom_list_tabs first.")
    _session["active"] = tab_id
    return f"[tab {tab_id}]\n{await _read()}"


@mcp.tool()
async def zerodom_close_tab(tab_id: str | None = None) -> str:
    """Close a tab — the active one by default. Refuses to close the last tab."""
    key = tab_id or _session["active"]
    if key not in _session["pages"]:
        raise ValueError(f"Unknown tab '{key}'. Call zerodom_list_tabs first.")
    if len(_session["pages"]) == 1:
        raise ValueError("Can't close the only open tab.")
    page = _session["pages"].pop(key)
    _session["cdp_sessions"].pop(key, None)
    _session["network_log"].pop(key, None)
    await page.close()
    if _session["active"] == key:
        _session["active"] = next(iter(_session["pages"]))
        await _read()
    return f"closed [tab {key}]\n\n{await zerodom_list_tabs()}"


@mcp.tool()
async def zerodom_screenshot(path: str | None = None) -> str:
    """Full-page screenshot of the active tab, saved to disk; returns the path.

    report.py has a screenshot path already, but it's wired to the CLI's own
    throwaway sync browser (playwright_wrapper.py's sync/async split), not
    this attached async session — this is that same capability for here.
    Pass `path` to choose where it's saved; omitted, a temp file is used.
    """
    page = await _page()
    if path is None:
        fd, path = tempfile.mkstemp(suffix=".png", prefix="zerodom-")
        os.close(fd)
    await page.screenshot(path=path, full_page=True)
    return f"saved to {path}"


@mcp.tool()
async def zerodom_set_viewport(width: int, height: int) -> str:
    """Resize the active tab's *viewport* for responsive-design testing.

    Not the real browser window — that's zerodom_resize_window. This is
    Emulation.setDeviceMetricsOverride: the page's own rendered layout at a
    given width, without touching the chrome around it. Use this one for
    "how does this render at width X"; use zerodom_resize_window for an
    actually different-sized window on screen.
    """
    page = await _page()
    await page.set_viewport_size({"width": width, "height": height})
    return await _read(diff=True)


@mcp.tool()
async def zerodom_resize_window(width: int, height: int) -> str:
    """Resize the real browser window (not just the page's viewport).

    chrome.debugger's CDP surface has no browser-level window-management
    grant, but chrome.windows.update is a plain extension API, entirely
    unrelated to chrome.debugger — so this genuinely works despite that.
    Sent as Browser.setWindowBounds (a real CDP method name Playwright's own
    driver will actually transmit — a made-up method name gets rejected
    client-side before reaching the relay at all) and repurposed server-side;
    see docs/DECISIONS.md D16. Only meaningful for an attached (real-browser)
    session; a launched headless session has no window to resize.

    On a tiling window manager (i3/sway/Hyprland/bspwm-style setups), this
    call succeeds but the window won't visibly move or resize — on Wayland
    compositors specifically this isn't a WM being uncooperative, it's the
    protocol itself: clients are deliberately not allowed to force their own
    geometry. Works normally on a floating window. Confirmed live on
    Hyprland.
    """
    page = await _page()
    active = _session["active"]
    cdp = _session["cdp_sessions"].get(active)
    if cdp is None:
        cdp = await page.context.new_cdp_session(page)
        _session["cdp_sessions"][active] = cdp
    # state: "normal" is required, not decorative — Chrome silently ignores
    # width/height in chrome.windows.update() while the window is maximized
    # (the common default), so a resize call would "succeed" and visibly do
    # nothing without this (confirmed live).
    bounds = {"width": width, "height": height, "state": "normal"}
    await cdp.send("Browser.setWindowBounds", {"windowId": 0, "bounds": bounds})
    return f"resized window to {width}x{height}"


# A curated subset of what getComputedStyle() returns (~300 properties) —
# the ones an actual design/CSS review asks about (layout, spacing, color,
# typography), not a full property dump most of which is irrelevant noise
# for any single element.
_COMPUTED_STYLE_JS = """(el) => {
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  const props = [
    'display', 'position', 'color', 'background-color', 'font-family',
    'font-size', 'font-weight', 'line-height', 'letter-spacing',
    'width', 'height', 'margin', 'padding', 'border', 'border-radius',
    'box-shadow', 'flex-direction', 'justify-content', 'align-items', 'gap',
    'z-index', 'opacity', 'visibility', 'overflow', 'text-align',
  ];
  const styles = {};
  for (const p of props) styles[p] = cs.getPropertyValue(p);
  return { box: { x: r.left, y: r.top, width: r.width, height: r.height }, styles };
}"""


@mcp.tool()
async def zerodom_get_styles(node_id: str) -> str:
    """Computed styles and box-model dimensions for a node — for design/CSS
    review, not just interaction.

    Runs getComputedStyle() in the real page over the same chrome.debugger
    connection everything else here uses (also reachable ad hoc via
    zerodom_eval_js; this is the purpose-built version with a curated
    property list instead of getComputedStyle()'s full ~300-property dump).
    """
    node = _node(node_id)
    page = await _page()
    locator = locate(page, node)
    result = await locator.evaluate(_COMPUTED_STYLE_JS)
    return json.dumps(result, indent=2)


@mcp.tool()
async def zerodom_get_cookies() -> str:
    """List cookies visible to the active tab's origin.

    Reads via CDP's Network domain (Playwright's context.cookies()), which
    sees httpOnly cookies too — unlike a content script's document.cookie,
    which httpOnly exists specifically to hide them from.
    """
    page = await _page()
    cookies = await page.context.cookies()
    if not cookies:
        return "No cookies."
    return "\n".join(
        f"{c['name']}={c['value']}  domain={c['domain']}  "
        f"httpOnly={c.get('httpOnly', False)}  secure={c.get('secure', False)}  "
        f"sameSite={c.get('sameSite', 'unspecified')}"
        for c in cookies
    )


@mcp.tool()
async def zerodom_eval_js(code: str) -> str:
    """Run arbitrary JavaScript in the active tab's real page context and
    return the result — Runtime.evaluate over the same chrome.debugger
    connection everything else here uses, the same power as typing into
    DevTools' own console.
    """
    page = await _page()
    result = await page.evaluate(code)
    try:
        return json.dumps(result)
    except TypeError:
        return str(result)


@mcp.tool()
async def zerodom_network_log(clear: bool = False) -> str:
    """Requests/responses the active tab has made since it opened (or since
    this was last called with clear=True) — method or status, and URL.

    Passive visibility only, capped at the most recent 200 entries — not
    interception or modification of traffic (that needs page.route(), a
    bigger, stateful feature; zerodom_eval_js can already override
    window.fetch/XMLHttpRequest from the page side for ad hoc cases).
    """
    await _page()  # ensure bootstrapped
    log = _session["network_log"].get(_session["active"], [])
    if not log:
        return "No requests captured yet."
    lines = [f"{e['type']:8} {e.get('method') or e.get('status')}  {e['url']}" for e in log]
    result = "\n".join(lines)
    if clear:
        _session["network_log"][_session["active"]] = []
    return result


async def _probe_relay_liveness(timeout: float = 3.0) -> str:
    """Bypasses Playwright and _page() entirely for a bounded-time answer to
    "is the extension actually connected" — the exact manual WebSocket probe
    that diagnosed a stuck extension handshake during development, where
    going through connect_over_cdp instead hung for 90+ seconds before it was
    interrupted. Opens and closes its own throwaway CDP connection, so it
    never disturbs an already-attached session.
    """
    import websockets

    from .relay import DEFAULT_PORT

    try:
        async with websockets.connect(f"ws://127.0.0.1:{DEFAULT_PORT}/cdp/local") as ws:
            await ws.send(json.dumps({"id": 0, "method": "Browser.getVersion", "params": {}}))
            await asyncio.wait_for(ws.recv(), timeout=timeout)
        return "extension connected, handshake OK"
    except asyncio.TimeoutError:
        return (
            "relay reachable but the extension handshake isn't completing — "
            "is the extension loaded and connected in the browser?"
        )
    except Exception as exc:
        return f"probe error: {exc}"


@mcp.tool()
async def zerodom_status() -> str:
    """Diagnose the current connection: relay reachability, whether a browser
    session is attached yet, which tab is active, and the tail of the relay's
    own log — the single-call version of the manual WebSocket-probing and log-
    tailing this project's own debugging needed before this tool existed.
    """
    from .relay import DEFAULT_PORT

    host = "127.0.0.1"
    relay_up = _relay_port_open(host, DEFAULT_PORT)
    lines = [
        f"relay:    {'reachable on ' + str(DEFAULT_PORT) if relay_up else 'NOT reachable'}",
        f"endpoint: {_cdp_endpoint() or '(not set)'}",
    ]

    active = _session["active"]
    if active is not None:
        page = _session["pages"][active]
        lines.append(
            f"session:  attached={_session['attached']}, active tab=[{active}], "
            f"open tabs={len(_session['pages'])}"
        )
        lines.append(f"active url: {page.url}")
    elif relay_up:
        lines.append(f"session:  not yet connected — {await _probe_relay_liveness()}")
    else:
        lines.append("session:  not yet connected")

    if _RELAY_LOG_PATH.exists():
        tail = _RELAY_LOG_PATH.read_text().splitlines()[-10:]
        if tail:
            lines.append("recent relay log:")
            lines.extend(f"  {line}" for line in tail)

    return "\n".join(lines)


def main() -> None:
    _ensure_relay_running()
    mcp.run()


if __name__ == "__main__":
    main()
