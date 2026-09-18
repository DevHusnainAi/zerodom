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
        if (params[0].tabId !== undefined) await markAttached(params[0].tabId);
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
  };

  ws.onerror = () => {
    stopPing();
    setStatus("disconnected");
  };
}

function disconnect() {
  if (ws) ws.close();
  ws = null;
  stopPing();
  setStatus("disconnected");
}

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
