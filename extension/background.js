// zerodom's own Chrome extension — Stage 6 (docs/DECISIONS.md D12). Speaks
// the exact protocol zerodom/relay.py's RelayServer already expects on its
// /extension/* endpoint: chrome.debugger.attach/detach/sendCommand and
// chrome.tabs.create/remove as incoming {id, method, params} commands,
// answered with {id, result} or {id, error}; chrome.tabs.onCreated/onRemoved
// and chrome.debugger.onEvent/onDetach pushed out as unsolicited {method,
// params} events, no id. No native messaging, no Microsoft extension in the
// loop — this connects straight to the relay's WebSocket.

let ws = null;
let status = "disconnected"; // disconnected | connecting | connected
let pingTimer = null;
let lastUrl = null;
let userDisconnected = false; // set only by an explicit popup click, not by a drop/error

// chrome.alarms, not setTimeout: an MV3 service worker can be evicted while
// idle, and a pending setTimeout does not survive that — it's just silently
// lost, so a failed first connect attempt could end up never retrying.
// Alarms are what Chrome guarantees will wake the worker back up to fire.
const RECONNECT_ALARM = "zerodom-reconnect";

// Matches relay.py's DEFAULT_PORT and run_relay()'s fixed id="local" — the
// same well-known address `zerodom-mcp` now auto-starts its relay on. No UI
// asks for this: the popup has no URL field, this is the only address ever used.
const DEFAULT_RELAY_URL = "ws://127.0.0.1:8765/extension/local";

// Tabs we've chrome.debugger.attach()'d, persisted in chrome.storage.session
// (survives an MV3 service-worker restart, unlike a plain JS Set) so a fresh
// connect() can always detach anything left over from a previous session
// before attaching again — otherwise Chrome rejects the re-attach with
// "Another debugger is already attached to the tab", and that stale state
// silently blocks every future attach on that tab until Chrome fully quits.
const ATTACHED_KEY = "zerodom-attached-tabs";

// Network log buffer — stores the last MAX_NETWORK_ENTRIES network events
// from the attached tab. Persisted in chrome.storage.session so it survives
// worker restarts, but capped to avoid bloat. The popup's Network tab reads
// this on demand.
const NETWORK_BUFFER_KEY = "zerodom-network-log";
const MAX_NETWORK_ENTRIES = 200;

async function markAttached(tabId) {
  const { [ATTACHED_KEY]: ids = [] } = await chrome.storage.session.get(ATTACHED_KEY);
  if (!ids.includes(tabId)) await chrome.storage.session.set({ [ATTACHED_KEY]: [...ids, tabId] });
  broadcastStatus();
}
async function markDetached(tabId) {
  const { [ATTACHED_KEY]: ids = [] } = await chrome.storage.session.get(ATTACHED_KEY);
  await chrome.storage.session.set({ [ATTACHED_KEY]: ids.filter((id) => id !== tabId) });
  broadcastStatus();
}
async function detachStaleTabs() {
  const { [ATTACHED_KEY]: ids = [] } = await chrome.storage.session.get(ATTACHED_KEY);
  for (const tabId of ids) {
    try { await chrome.debugger.detach({ tabId }); } catch (e) {}
  }
  await chrome.storage.session.set({ [ATTACHED_KEY]: [] });
}

// ─── Network log buffer ───────────────────────────────────────────────────
// Captures Network.requestWillBeSent and Network.responseReceived from the
// attached tab. The popup's Network tab reads this on demand. Capped at
// MAX_NETWORK_ENTRIES to avoid storage bloat.

async function pushNetworkEntry(entry) {
  const { [NETWORK_BUFFER_KEY]: entries = [] } = await chrome.storage.session.get(NETWORK_BUFFER_KEY);
  entries.push(entry);
  // Trim to cap — drop oldest first
  if (entries.length > MAX_NETWORK_ENTRIES) {
    entries.splice(0, entries.length - MAX_NETWORK_ENTRIES);
  }
  await chrome.storage.session.set({ [NETWORK_BUFFER_KEY]: entries });
}

async function getNetworkLog() {
  const { [NETWORK_BUFFER_KEY]: entries = [] } = await chrome.storage.session.get(NETWORK_BUFFER_KEY);
  return entries;
}

async function clearNetworkLog() {
  await chrome.storage.session.set({ [NETWORK_BUFFER_KEY]: [] });
}

// ─── Cookies via CDP ──────────────────────────────────────────────────────
// Uses Network.getCookies — same CDP call Playwright uses for
// context.cookies(). Returns cookies for the current page URL only,
// including httpOnly cookies that content scripts cannot see.

async function getCookies() {
  const tabId = await getAttachedTabId();
  if (tabId === undefined) return { attached: false, cookies: [] };
  try {
    const res = await chrome.debugger.sendCommand(
      { tabId }, "Network.getCookies"
    );
    return { attached: true, cookies: res.cookies || [] };
  } catch (e) {
    return { attached: true, cookies: [], error: e.message || String(e) };
  }
}

// ─── Web storage via CDP (F11) ────────────────────────────────────────────
// Cookies are httpOnly-visible but limited to Network.getCookies' cookie jar;
// localStorage/sessionStorage need to run in the page context, which is what
// this Runtime.evaluate does over the same debugger connection. Same power as
// the DevTools console — deliberately identical to what zerodom_eval_js does
// server-side, but purpose-built so the popup can render it.
async function getWebStorage() {
  const tabId = await getAttachedTabId();
  if (tabId === undefined) return { attached: false, local: [], session: [] };
  try {
    const res = await chrome.debugger.sendCommand(
      { tabId }, "Runtime.evaluate", {
        expression: `(() => {
          const read = (s) => {
            try {
              return Object.keys(s).map((k) => ({ key: k, value: s[k] }));
            } catch (e) { return [{ key: "(unreadable)", value: String(e) }]; }
          };
          return { local: read(window.localStorage), session: read(window.sessionStorage) };
        })()`,
        returnByValue: true,
      }
    );
    const v = res.result && res.result.value;
    return {
      attached: true,
      local: (v && v.local) || [],
      session: (v && v.session) || [],
    };
  } catch (e) {
    return { attached: true, local: [], session: [], error: e.message || String(e) };
  }
}

// ─── Locator validation (F6) ─────────────────────────────────────────────
// Batch querySelectorAll count check for the popup's Graph tab: given a list
// of selectors, returns how many live elements each matches — empirical
// "ok (1) / ambiguous (N) / dead (0) / invalid" verdicts in one round-trip,
// instead of the predictive pattern-based stability dot. Mirrors what
// mcp_server.py's on-page sidebar does per-click (its __zerodomRenderList
// verdict logic) but amortized across the whole graph in a single
// Runtime.evaluate call.
async function validateSelectors(selectors) {
  const tabId = await getAttachedTabId();
  if (tabId === undefined) return { attached: false, counts: {} };
  try {
    const selList = Array.isArray(selectors) ? selectors : [];
    const js = `(() => {
      const selList = ${JSON.stringify(selList)};
      const out = {};
      for (const s of selList) {
        try { out[s] = document.querySelectorAll(s).length; }
        catch (e) { out[s] = -1; }
      }
      return out;
    })()`;
    const res = await chrome.debugger.sendCommand(
      { tabId }, "Runtime.evaluate", { expression: js, returnByValue: true }
    );
    return { attached: true, counts: res.result.value || {} };
  } catch (e) {
    return { attached: true, counts: {}, error: e.message || String(e) };
  }
}

// ─── DOM ancestry for the Tree view ──────────────────────────────────────
// The graph is a flat node list — the parser emits no parent links — so the
// tree is reconstructed from the live DOM. One Runtime.evaluate for the whole
// selector set, same batching reason as validateSelectors: the popup pays CDP
// latency per message, and a walk per node would be hundreds of round-trips.
// Keys are cached on the element so one container yields one key across every
// selector beneath it, which is what makes a shared ancestor detectable.
async function getAncestry(selectors) {
  const tabId = await getAttachedTabId();
  if (tabId === undefined) return { attached: false, paths: {}, labels: {} };
  try {
    const js = `(() => {
      const selList = ${JSON.stringify(Array.isArray(selectors) ? selectors : [])};
      const paths = {}, labels = {};
      let seq = 0;
      for (const sel of selList) {
        let el;
        try { el = document.querySelector(sel); } catch (e) { continue; }
        if (!el) continue;
        const chain = [];
        for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
          if (p.id && p.id.indexOf('zerodom-') === 0) break;
          if (!p.__zdKey) {
            p.__zdKey = 'k' + (++seq);
            const id = p.id ? '#' + p.id : '';
            const cls = (!id && p.classList.length) ? '.' + p.classList[0] : '';
            labels[p.__zdKey] = p.localName + id + cls;
          }
          chain.unshift(p.__zdKey);
        }
        paths[sel] = chain;
      }
      return JSON.stringify({ paths, labels });
    })()`;
    const res = await chrome.debugger.sendCommand(
      { tabId }, "Runtime.evaluate", { expression: js, returnByValue: true }
    );
    const v = JSON.parse(res.result.value);
    return { attached: true, paths: v.paths, labels: v.labels };
  } catch (e) {
    return { attached: true, paths: {}, labels: {}, error: e.message || String(e) };
  }
}

// ─── Graph data from driving tab ──────────────────────────────────────────
// Reads the interaction graph that mcp_server.py already computed and stored
// in window.__zerodomNodes. Also reads token comparison stats if available.

const GRAPH_JS = `(() => {
  const nodes = window.__zerodomNodes || [];
  const compTokens = window.__zerodomCompTokens || null;
  const ariaTokens = window.__zerodomAriaTokens || null;
  return JSON.stringify({ nodes, compTokens, ariaTokens });
})()`;

// ─── Page-click capture (F1, bidirectional selection) ────────────────────
// Installs a capture-phase click listener in the real page that maps the
// element the user actually clicked onto the graph node it belongs to
// (querySelector against __zerodomNodes' selectors — same selectors mcp_server
// generated, so it can't drift from what the graph lists). Pushes matched
// node ids into window.__zerodomClicked; the popup drains the queue on its
// next graph refresh and flashes the matching sidebar rows. Idempotent (a
// per-navigation reload starts a fresh page context, so the guard flag resets
// on its own; SPA soft-navigation keeps the listener because it never reloads).
async function ensureClickCapture() {
  const tabId = await getAttachedTabId();
  if (tabId === undefined) return { attached: false };
  try {
    const js = `(() => {
      if (window.__zerodomClicked) return { installed: true };
      window.__zerodomClicked = [];
      document.addEventListener('click', (e) => {
        const clicked = e.target;
        if (!clicked || !clicked.closest) return;
        // Clicking zerodom's own sidebar/driving UI must not self-refer.
        if (clicked.closest('[id^="zerodom-"], [data-zerodom-ignore]')) return;
        const nodes = window.__zerodomNodes || [];
        for (const n of nodes) {
          if (!n.selector) continue;
          try {
            const el = document.querySelector(n.selector);
            if (el && (el === clicked || el.contains(clicked))) {
              window.__zerodomClicked.push(n.id);
              break;
            }
          } catch (err) { /* bad selector — skip node */ }
        }
      }, true);
      return { installed: true };
    })()`;
    await chrome.debugger.sendCommand({ tabId }, "Runtime.evaluate", { expression: js, returnByValue: true });
    return { attached: true };
  } catch (e) {
    return { attached: true, error: e.message || String(e) };
  }
}

async function drainClicked() {
  const tabId = await getAttachedTabId();
  if (tabId === undefined) return { attached: false, ids: [] };
  try {
    const js = `(() => {
      const q = window.__zerodomClicked || [];
      window.__zerodomClicked = [];
      return JSON.stringify(q);
    })()`;
    const res = await chrome.debugger.sendCommand({ tabId }, "Runtime.evaluate", { expression: js, returnByValue: true });
    let ids = [];
    try { ids = JSON.parse(res.result.value); } catch (e) { /* ignore */ }
    return { attached: true, ids: Array.isArray(ids) ? ids : [] };
  } catch (e) {
    return { attached: true, ids: [], error: e.message || String(e) };
  }
}

// ─── Highlight node overlay ───────────────────────────────────────────────
// Creates/removes a simple red outline around the element matching selector.
// Runs in page context via Runtime.evaluate.
async function highlightNode(selector, clear = false) {
  const tabId = await getAttachedTabId();
  if (tabId === undefined) return { attached: false };
  try {
    const js = `(() => {
      const key = '__zerodomHighlightOverlay';
      const remove = () => {
        const old = document.getElementById(key);
        if (old) old.remove();
      };
      remove();
      if (${clear}) return { ok: true, cleared: true };
      try {
        const el = document.querySelector(${JSON.stringify(selector)});
        if (!el) return { ok: false, error: 'no element' };
        const rect = el.getBoundingClientRect();
        const box = document.createElement('div');
        box.id = key;
        box.style.position = 'fixed';
        box.style.left = rect.left + 'px';
        box.style.top = rect.top + 'px';
        box.style.width = rect.width + 'px';
        box.style.height = rect.height + 'px';
        box.style.border = '2px solid #22e0d8';
        box.style.boxSizing = 'border-box';
        box.style.pointerEvents = 'none';
        box.style.zIndex = 2147483647;
        box.style.boxShadow = '0 0 0 2px rgba(34,224,216,.25) inset';
        document.body.appendChild(box);
        return { ok: true, rect };
      } catch (e) {
        return { ok: false, error: String(e) };
      }
    })()`;
    const res = await chrome.debugger.sendCommand({ tabId }, "Runtime.evaluate", { expression: js, returnByValue: true });
    return { attached: true, ...res.result.value };
  } catch (e) {
    return { attached: true, error: e.message || String(e) };
  }
}

async function actNode(selector, action = 'click') {
  const tabId = await getAttachedTabId();
  if (tabId === undefined) return { attached: false };
  try {
    const js = `(() => {
      try {
        const el = document.querySelector(${JSON.stringify(selector)});
        if (!el) return { ok: false, error: 'no element' };
        if (${JSON.stringify(action)} === 'click') {
          el.click();
          return { ok: true, action: 'click' };
        }
        if (${JSON.stringify(action)} === 'focus') {
          el.focus();
          return { ok: true, action: 'focus' };
        }
        return { ok: false, error: 'unsupported action' };
      } catch (e) {
        return { ok: false, error: String(e) };
      }
    })()`;
    const res = await chrome.debugger.sendCommand({ tabId }, "Runtime.evaluate", { expression: js, returnByValue: true });
    return { attached: true, ...res.result.value };
  } catch (e) {
    return { attached: true, error: e.message || String(e) };
  }
}

async function getGraphData() {
  const tabId = await getAttachedTabId();
  if (tabId === undefined) return { attached: false, nodes: [], compTokens: null, ariaTokens: null };
  try {
    const res = await chrome.debugger.sendCommand(
      { tabId }, "Runtime.evaluate", { expression: GRAPH_JS, returnByValue: true }
    );
    const parsed = JSON.parse(res.result.value);
    return { attached: true, ...parsed };
  } catch (e) {
    return { attached: true, nodes: [], compTokens: null, ariaTokens: null, error: e.message || String(e) };
  }
}

// ─── Page URL + tab actions ───────────────────────────────────────────────

async function getPageUrl() {
  const tabId = await getAttachedTabId();
  if (tabId === undefined) return { url: null };
  try {
    const tab = await chrome.tabs.get(tabId);
    return { url: tab.url };
  } catch (e) {
    return { url: null };
  }
}

async function openNewTab(url) {
  if (!url) return { error: "No URL" };
  try {
    const tab = await chrome.tabs.create({ url });
    return { ok: true, tabId: tab.id };
  } catch (e) {
    return { error: e.message || String(e) };
  }
}

async function takeScreenshot() {
  const tabId = await getAttachedTabId();
  if (tabId === undefined) return { error: "No attached tab" };
  try {
    const res = await chrome.debugger.sendCommand(
      { tabId }, "Page.captureScreenshot", { format: "png" }
    );
    return { data: res.data };
  } catch (e) {
    return { error: e.message || String(e) };
  }
}

// Popup features below (live stats, HUD toggle) read/act on the driving
// tab's own already-rendered bar (mcp_server.py's _DRIVING_UI_JS) through
// Runtime.evaluate over the *same* chrome.debugger attachment the relay
// already holds — no new permission, no new server/relay protocol message,
// just asking the page what it already shows.
async function getAttachedTabId() {
  const { [ATTACHED_KEY]: ids = [] } = await chrome.storage.session.get(ATTACHED_KEY);
  return ids[0]; // ponytail: one global driving session (mcp_server.py), so at most one
}

const LIVE_STATS_JS = "(() => {" +
  "const val = (id) => document.getElementById(id)?.textContent || null;" +
  "return {" +
    "hasBar: !!document.getElementById('zerodom-driving-bar')," +
    "target: val('zerodom-bar-target')," +
    "path: val('zerodom-bar-path')," +
    "tokens: val('zerodom-bar-tokens')," +
    "step: val('zerodom-bar-step')," +
  "};" +
"})()";

async function getLiveStats() {
  const tabId = await getAttachedTabId();
  if (tabId === undefined) return { attached: false };
  try {
    const res = await chrome.debugger.sendCommand(
      { tabId }, "Runtime.evaluate", { expression: LIVE_STATS_JS, returnByValue: true }
    );
    return { attached: true, ...res.result.value };
  } catch (e) {
    return { attached: true, error: e.message || String(e) };
  }
}

// ponytail: toggling only affects the bar already on the page right now --
// it isn't a stored preference the server re-checks on the next navigation,
// so the bar comes back on the next page. Upgrade path if that's ever
// annoying: have mcp_server.py's _DRIVING_UI_JS check a flag (e.g. in
// localStorage) before drawing itself, set by this same toggle.
// Returns {error} rather than pretending success when the bar doesn't exist
// yet (e.g. mcp_server.py hasn't drawn it, or drawing it failed) -- treating
// "no bar" as "successfully hidden" used to report hidden:true on every
// click with nothing to toggle back, which left the popup's switch stuck off
// forever with no way to turn it back on.
const TOGGLE_HUD_JS = "(() => {" +
  "const bar = document.getElementById('zerodom-driving-bar');" +
  "if (!bar) return { error: 'no driving bar on this page yet' };" +
  "const barHeight = bar.getBoundingClientRect().height;" +
  "const hiding = bar.style.display !== 'none';" +
  "['zerodom-driving-bar', 'zerodom-driving-border', 'zerodom-log-panel'].forEach((id) => {" +
    "const el = document.getElementById(id);" +
    "if (el) el.style.display = hiding ? 'none' : '';" +
  "});" +
  "document.body.style.marginTop = hiding ? '0px' : barHeight + 'px';" +
  "return { hidden: hiding };" +
"})()";

async function toggleHud() {
  const tabId = await getAttachedTabId();
  if (tabId === undefined) return { attached: false };
  try {
    const res = await chrome.debugger.sendCommand(
      { tabId }, "Runtime.evaluate", { expression: TOGGLE_HUD_JS, returnByValue: true }
    );
    const value = res.result.value;
    if (value && value.error) return { attached: true, error: value.error };
    return { attached: true, hidden: value.hidden };
  } catch (e) {
    return { attached: true, error: e.message || String(e) };
  }
}

// Real, honest kill switch, not decoration: the on-page bar can't call
// chrome.debugger.detach() itself -- it's plain page JS via
// page.evaluate(), with no chrome.* access -- so the only place a stop
// control can actually live is here, the extension, which can. Detaching
// directly from the extension has no dependency on the relay or the MCP
// server being alive or responsive at all -- the same property Chrome's
// own native debugging-infobar Cancel button has, and why *that* one still
// works no matter what's stuck.
const STOP_INDICATOR_JS = "(() => {" +
  "const el = document.getElementById('zerodom-bar-active');" +
  "if (el) el.innerHTML = '<span style=\"width:6px;height:6px;border-radius:50%;" +
    "background:#e05252;flex:none;\"></span>STOPPED';" +
  "const bar = document.getElementById('zerodom-driving-bar');" +
  "if (bar) bar.style.borderBottomColor = 'rgba(224,82,82,.4)';" +
"})()";

async function emergencyStop() {
  const tabId = await getAttachedTabId();
  if (tabId === undefined) return { attached: false };
  try {
    // Best-effort cosmetic update -- if the page navigated or the bar
    // isn't there this just fails quietly, the detach below is what
    // actually matters and always runs regardless.
    try {
      await chrome.debugger.sendCommand({ tabId }, "Runtime.evaluate", { expression: STOP_INDICATOR_JS });
    } catch (e) {}
    await chrome.debugger.detach({ tabId });
    await markDetached(tabId);
    return { attached: true, stopped: true };
  } catch (e) {
    return { attached: true, error: e.message || String(e) };
  }
}

// One persistent tab group for every tab zerodom touches — matches
// claude-in-chrome's UX (a visibly grouped, colored, titled set of tabs) so
// the user can tell at a glance which tabs are under agent control. zerodom
// is a single global session (see mcp_server.py's "ponytail" comment), so
// unlike claude-in-chrome there's exactly one group, not one per conversation.
const GROUP_KEY = "zerodom-group-id";

async function ensureGrouped(tabId) {
  const { [GROUP_KEY]: groupId } = await chrome.storage.session.get(GROUP_KEY);
  if (groupId !== undefined) {
    try {
      const newGroupId = await chrome.tabs.group({ tabIds: [tabId], groupId });
      if (newGroupId !== groupId) await chrome.storage.session.set({ [GROUP_KEY]: newGroupId });
      return;
    } catch (e) {
      // Stored group no longer exists (user closed/ungrouped it) — fall through and recreate.
    }
  }
  const newGroupId = await chrome.tabs.group({ tabIds: [tabId] });
  await chrome.tabGroups.update(newGroupId, { title: "zerodom", color: "cyan" });
  await chrome.storage.session.set({ [GROUP_KEY]: newGroupId });
}

// MV3 service workers get killed after ~30s idle; an open WebSocket alone
// doesn't reliably prevent that in every Chrome version. A small periodic
// ping keeps real traffic flowing so Chrome doesn't reclaim us mid-session
// -- the relay just ignores it (no "id", method matches nothing it handles).
function startPing() {
  stopPing();
  pingTimer = setInterval(() => sendEvent("extension.ping", []), 20000);
}
function stopPing() {
  if (pingTimer) clearInterval(pingTimer);
  pingTimer = null;
}

// So the popup can show *which* tab zerodom is driving, not just that it's
// connected -- reads the same ATTACHED_KEY markAttached/markDetached keep
// current, so this stays right even across a service-worker restart.
async function getAttachedTabInfo() {
  const { [ATTACHED_KEY]: ids = [] } = await chrome.storage.session.get(ATTACHED_KEY);
  const tabs = [];
  for (const tabId of ids) {
    try {
      const tab = await chrome.tabs.get(tabId);
      tabs.push({ id: tabId, title: tab.title, url: tab.url });
    } catch (e) {
      // Tab closed underneath us; markDetached's onRemoved listener will
      // clean ATTACHED_KEY up shortly, nothing to report meanwhile.
    }
  }
  return tabs;
}

async function broadcastStatus() {
  const tabs = await getAttachedTabInfo();
  chrome.runtime.sendMessage({ type: "zerodom-status", status, tabs }).catch(() => {});
}

function setStatus(next) {
  status = next;
  broadcastStatus();
}

// ─── Outgoing: chrome.* events pushed to the relay, unsolicited ───────────

function sendEvent(method, params) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ method, params }));
  }
}

function onTabCreated(tab) {
  sendEvent("chrome.tabs.onCreated", [tab]);
}
function onTabRemoved(tabId, removeInfo) {
  sendEvent("chrome.tabs.onRemoved", [tabId, removeInfo]);
}
function onDebuggerEvent(source, method, params) {
  // Capture network events for the popup's Network tab — in parallel with
  // forwarding to the relay. Only captures request/response pairs, not
  // every CDP event.
  if (method === "Network.requestWillBeSent") {
    const req = params.request;
    pushNetworkEntry({
      type: "request",
      method: req.method,
      url: req.url,
      timestamp: params.timestamp,
    });
  } else if (method === "Network.responseReceived") {
    const res = params.response;
    pushNetworkEntry({
      type: "response",
      method: res.requestId ? "response" : res.protocol,
      status: res.status,
      url: res.url,
      timestamp: params.timestamp,
    });
  }
  sendEvent("chrome.debugger.onEvent", [source, method, params ?? {}]);
}
function onDebuggerDetach(source, reason) {
  if (source.tabId !== undefined) markDetached(source.tabId);
  sendEvent("chrome.debugger.onDetach", [source, reason]);
}

// ─── Incoming: commands from the relay, each needs an {id, result|error} ──

async function handleCommand(id, method, params) {
  try {
    let result;
    switch (method) {
      case "chrome.debugger.attach":
        await chrome.debugger.attach(params[0], params[1]);
        if (params[0].tabId !== undefined) {
          await markAttached(params[0].tabId);
          await ensureGrouped(params[0].tabId);
          // Enable Network domain so we can capture request/response events
          // for the popup's Network tab. Best-effort: if this fails the tab
          // still works, we just won't see network entries.
          try {
            await chrome.debugger.sendCommand(
              { tabId: params[0].tabId }, "Network.enable"
            );
          } catch (e) {}
        }
        result = null;
        break;
      case "chrome.debugger.detach":
        await chrome.debugger.detach(params[0]);
        if (params[0].tabId !== undefined) await markDetached(params[0].tabId);
        result = null;
        break;
      case "chrome.debugger.sendCommand":
        result = await chrome.debugger.sendCommand(params[0], params[1], params[2]);
        break;
      case "chrome.tabs.create":
        result = await chrome.tabs.create(params[0]);
        await ensureGrouped(result.id);
        break;
      case "chrome.tabs.remove":
        await chrome.tabs.remove(params[0]);
        result = null;
        break;
      case "chrome.windows.update": {
        // Real window resize -- chrome.windows is a plain extension API,
        // unrelated to chrome.debugger/CDP entirely, so this works despite
        // chrome.debugger's own window-management limits.
        const [tabId, updateInfo] = params;
        const tab = await chrome.tabs.get(tabId);
        result = await chrome.windows.update(tab.windowId, updateInfo);
        break;
      }
      default:
        throw new Error(`unknown method: ${method}`);
    }
    ws.send(JSON.stringify({ id, result }));
  } catch (err) {
    ws.send(JSON.stringify({ id, error: err.message || String(err) }));
  }
}

// ─── Connection lifecycle ──────────────────────────────────────────────

async function connect(url) {
  lastUrl = url;
  userDisconnected = false;
  chrome.alarms.clear(RECONNECT_ALARM);
  if (ws) {
    try { ws.close(); } catch (e) {}
  }
  setStatus("connecting");
  await detachStaleTabs();
  ws = new WebSocket(url);

  ws.onopen = async () => {
    // Initial handshake the relay's BrowserModel expects: push every tab we
    // already know about, then signal we're done — see relay.py's
    // BrowserModel docstring on why order matters here.
    const tabs = await chrome.tabs.query({});
    for (const tab of tabs) onTabCreated(tab);
    sendEvent("extension.initialized", []);
    setStatus("connected");
    startPing();
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id !== undefined && msg.method !== undefined) {
      handleCommand(msg.id, msg.method, msg.params);
    }
  };

  ws.onclose = () => {
    ws = null;
    stopPing();
    setStatus("disconnected");
    scheduleReconnect();
  };

  ws.onerror = () => {
    stopPing();
    setStatus("disconnected");
  };
}

// The relay may not be up yet the moment Chrome starts (zerodom-mcp hasn't
// run yet) — retry instead of giving up, so "just works" doesn't depend on
// launch order. Stops the moment the user explicitly disconnects. Chrome
// won't honor an alarm sooner than ~1 minute, which is fine here: this is a
// background safety net, not the primary path (autoConnect() already tries
// immediately on every startup/install/wake).
function scheduleReconnect() {
  if (userDisconnected) return;
  chrome.alarms.create(RECONNECT_ALARM, { delayInMinutes: 1 });
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RECONNECT_ALARM && !userDisconnected && lastUrl) connect(lastUrl);
});

function disconnect() {
  userDisconnected = true;
  chrome.alarms.clear(RECONNECT_ALARM);
  if (ws) ws.close();
  ws = null;
  stopPing();
  setStatus("disconnected");
}

// ─── Auto-connect on browser/service-worker startup ────────────────────
// No popup click needed: the relay lives at a fixed, well-known address, and
// zerodom-mcp auto-starts it on its own (see mcp_server.py's
// _ensure_relay_running). Whichever agent (Claude, opencode, Codex, ...)
// calls zerodom-mcp brings the relay up; this just keeps trying to reach it.
//
// One call site only, deliberately: MV3 re-runs this whole script top-to-
// bottom on every wake (install, reload, browser startup, post-eviction
// restart alike) — chrome.runtime.onStartup/onInstalled are NOT extra
// wake conditions here, they're the same wake firing a second/third time.
// Wiring both used to race two overlapping connect() calls, where sendEvent()
// sent extension.initialized down whichever WebSocket the shared `ws`
// variable pointed to *last* — not necessarily the one that actually opened
// — so the popup showed "connected" while the relay never heard the
// handshake at all.
connect(DEFAULT_RELAY_URL);

chrome.debugger.onEvent.addListener(onDebuggerEvent);
chrome.debugger.onDetach.addListener(onDebuggerDetach);
chrome.tabs.onCreated.addListener(onTabCreated);
chrome.tabs.onRemoved.addListener(onTabRemoved);

// ─── Popup ↔ background messaging ──────────────────────────────────────

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "zerodom-connect") {
    connect(message.url);
    sendResponse({ ok: true });
  } else if (message.type === "zerodom-disconnect") {
    disconnect();
    sendResponse({ ok: true });
  } else if (message.type === "zerodom-get-status") {
    getAttachedTabInfo().then((tabs) => sendResponse({ status, tabs }));
  } else if (message.type === "zerodom-get-live-stats") {
    getLiveStats().then(sendResponse);
  } else if (message.type === "zerodom-toggle-hud") {
    toggleHud().then(sendResponse);
  } else if (message.type === "zerodom-emergency-stop") {
    emergencyStop().then(sendResponse);
  } else if (message.type === "zerodom-get-network-log") {
    getNetworkLog().then(sendResponse);
  } else if (message.type === "zerodom-get-cookies") {
    getCookies().then(sendResponse);
  } else if (message.type === "zerodom-get-storage") {
    getWebStorage().then(sendResponse);
  } else if (message.type === "zerodom-get-graph") {
    getGraphData().then(sendResponse);
  } else if (message.type === "zerodom-ensure-click-capture") {
    ensureClickCapture().then(sendResponse);
  } else if (message.type === "zerodom-drain-clicked") {
    drainClicked().then(sendResponse);
  } else if (message.type === "zerodom-validate-selectors") {
    validateSelectors(message.selectors).then(sendResponse);
  } else if (message.type === "zerodom-get-ancestry") {
    getAncestry(message.selectors).then(sendResponse);
  } else if (message.type === "zerodom-clear-network-log") {
    clearNetworkLog().then(() => sendResponse({ ok: true }));
  } else if (message.type === "zerodom-get-page-url") {
    getPageUrl().then(sendResponse);
  } else if (message.type === "zerodom-open-new-tab") {
    openNewTab(message.url).then(sendResponse);
  } else if (message.type === "zerodom-screenshot") {
    takeScreenshot().then(sendResponse);
  } else if (message.type === "zerodom-highlight-node") {
    highlightNode(message.selector, message.clear).then(sendResponse);
  } else if (message.type === "zerodom-act-node") {
    actNode(message.selector, message.action).then(sendResponse);
  }
  return true;
});
