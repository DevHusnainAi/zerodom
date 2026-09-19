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

async function markAttached(tabId) {
  const { [ATTACHED_KEY]: ids = [] } = await chrome.storage.session.get(ATTACHED_KEY);
  if (!ids.includes(tabId)) await chrome.storage.session.set({ [ATTACHED_KEY]: [...ids, tabId] });
}
async function markDetached(tabId) {
  const { [ATTACHED_KEY]: ids = [] } = await chrome.storage.session.get(ATTACHED_KEY);
  await chrome.storage.session.set({ [ATTACHED_KEY]: ids.filter((id) => id !== tabId) });
}
async function detachStaleTabs() {
  const { [ATTACHED_KEY]: ids = [] } = await chrome.storage.session.get(ATTACHED_KEY);
  for (const tabId of ids) {
    try { await chrome.debugger.detach({ tabId }); } catch (e) {}
  }
  await chrome.storage.session.set({ [ATTACHED_KEY]: [] });
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

function broadcastStatus() {
  chrome.runtime.sendMessage({ type: "zerodom-status", status }).catch(() => {});
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
    sendResponse({ status });
  }
  return true;
});
