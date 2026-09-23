// ─── ZeroDOM Extension Popup ─────────────────────────────────────────────
// Deterministic DOM inspector popup — session, HUD, network, cookies, graph

const DEFAULT_RELAY_URL = "ws://127.0.0.1:8765/extension/local";
const REPO_URL = "https://github.com/DevHusnainAi/zerodom";

// ─── DOM refs ────────────────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const actionBtn = $("#action");
const dot = $(".dot");
const statusText = $("#status-text");
const tabsEl = $("#tabs");
const liveStatsEl = $("#live-stats");
const hudToggle = $("#hud-toggle");
const stopBtn = $("#stop-btn");
const stopHint = $("#stop-hint");
const netSummary = $("#net-summary");
const netFilter = $("#net-filter");
const netList = $("#net-list");
const cookieSummary = $("#cookie-summary");
const cookieList = $("#cookie-list");
const toastEl = $("#toast");

// ─── Helpers ─────────────────────────────────────────────────────────────

function send(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, resolve);
  });
}

function toast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add("show");
  setTimeout(() => toastEl.classList.remove("show"), 1200);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied");
  } catch (e) {
    toast("Copy failed");
  }
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

function row(k, v) {
  return `<div class="row"><span class="k">${escapeHtml(k)}</span>` +
    `<span class="v mono">${escapeHtml(v)}</span></div>`;
}

// ─── Nav ────────────────────────────────────────────────────────────────

$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".nav-item").forEach((b) => b.classList.remove("active"));
    $$("section").forEach((s) => s.classList.remove("active"));
    btn.classList.add("active");
    $("#panel-" + btn.dataset.panel).classList.add("active");
    if (btn.dataset.panel === "session") refreshLiveStats();
    if (btn.dataset.panel === "network") refreshNetwork();
    if (btn.dataset.panel === "cookies") { refreshCookies(); refreshStorage(); }
    if (btn.dataset.panel === "graph") refreshGraph();
  });
});

// ─── Session tab ────────────────────────────────────────────────────────

let currentStatus = "disconnected";
let currentUrl = null;

function renderSession(status, tabs) {
  statusText.textContent = status;
  dot.className = "dot" + (status === "connected" ? " on" : "");
  if (status === "connected") {
    actionBtn.textContent = "Disconnect";
    actionBtn.className = "danger";
  } else if (status === "connecting") {
    actionBtn.textContent = "Connecting…";
    actionBtn.className = "";
  } else {
    actionBtn.textContent = "Connect";
    actionBtn.className = "";
  }
  renderTabs(tabs);
  if (tabs && tabs.length) currentUrl = tabs[0].url || currentUrl;
}

function renderTabs(tabs) {
  if (!tabs || tabs.length === 0) {
    tabsEl.textContent = currentStatus === "connected"
      ? "not driving a tab yet"
      : "";
    return;
  }
  tabsEl.innerHTML = tabs.map((t) => {
    const groupBadge = t.groupColor
      ? `<span class="tab-group-badge" style="background:${escapeHtml(t.groupColor)}"></span>`
      : "";
    const title = escapeHtml(t.title || t.url);
    return `<div class="tab-row">${groupBadge}` +
      `<span class="tab-title">${title}</span>` +
      `<button class="tab-close" data-tab-id="${escapeHtml(t.id)}">×</button>` +
      `</div>`;
  }).join("");

  $$(".tab-close").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      send({ type: "zerodom-close-tab", tabId: btn.dataset.tabId });
    });
  });
}

function renderLiveStats(stats) {
  const attached = !!(stats && stats.attached);
  stopBtn.style.display = attached ? "" : "none";
  stopHint.style.display = attached ? "" : "none";

  if (!attached) {
    liveStatsEl.innerHTML = "";
    return;
  }
  if (!stats.hasBar) {
    liveStatsEl.innerHTML = `<p class="hint">No action taken yet this session.</p>`;
    return;
  }

  const statsFields = [
    ["target", stats.target],
    ["path", stats.path],
    ["tokens", stats.tokens],
    ["step", stats.step],
    ["mcp calls", stats.mcpCalls],
    ["clicks", stats.clicks],
    ["fills", stats.fills],
    ["types", stats.typing],
    ["hovers", stats.hovers],
    ["scrolls", stats.scrolls],
  ];
  liveStatsEl.innerHTML = '<div class="panel">' +
    statsFields
      .filter(([, v]) => v != null)
      .map(([k, v]) => row(k, String(v)))
      .join("") +
    "</div>";
}

async function refreshLiveStats() {
  const res = await send({ type: "zerodom-get-live-stats" });
  renderLiveStats(res);
}

actionBtn.addEventListener("click", async () => {
  if (actionBtn.textContent === "Disconnect") {
    await send({ type: "zerodom-disconnect" });
  } else {
    await send({ type: "zerodom-connect", url: DEFAULT_RELAY_URL });
  }
});

stopBtn.addEventListener("click", async () => {
  stopBtn.disabled = true;
  stopBtn.textContent = "Stopping…";
  const res = await send({ type: "zerodom-emergency-stop" });
  stopBtn.disabled = false;
  stopBtn.textContent = "Stop — detach now";
  if (res && res.stopped) {
    refreshLiveStats();
  }
});

// ─── HUD tab ─────────────────────────────────────────────────────────────

hudToggle.addEventListener("click", async () => {
  const res = await send({ type: "zerodom-toggle-hud" });
  if (res && res.attached && !res.error) {
    hudToggle.classList.toggle("on", !res.hidden);
  }
});

// ─── About tab ───────────────────────────────────────────────────────────

$("#ext-version").textContent = chrome.runtime.getManifest().version;
$("#feature-link").href = REPO_URL + "/issues/new";

// ─── Network tab ─────────────────────────────────────────────────────────

let networkEntries = [];

function mergeNetworkEntries(entries) {
  const byUrl = new Map();
  for (const e of entries) {
    const key = e.url || "";
    if (!byUrl.has(key)) {
      byUrl.set(key, { url: key, method: null, status: null, time: null, responseHeaders: null });
    }
    const merged = byUrl.get(key);
    if (e.type === "request") {
      merged.method = e.method;
      merged.time = e.time;
      merged.responseHeaders = e.responseHeaders;
    }
    if (e.type === "response" || e.status) {
      merged.status = e.status || e.responseStatus;
    }
    if (e.method) merged.method = e.method;
    if (e.status) merged.status = e.status;
    if (e.time) merged.time = e.time;
  }
  return Array.from(byUrl.values());
}

function statusBadgeClass(status) {
  if (!status) return "badge-unknown";
  if (status >= 200 && status < 300) return "badge-2xx";
  if (status >= 300 && status < 400) return "badge-3xx";
  if (status >= 400 && status < 500) return "badge-4xx";
  return "badge-5xx";
}

function renderNetwork(entries) {
  const merged = mergeNetworkEntries(entries || []);
  networkEntries = merged;
  applyNetworkFilter();
}

function applyNetworkFilter() {
  const q = (netFilter.value || "").toLowerCase();
  const filtered = q
    ? networkEntries.filter((e) => e.url.toLowerCase().includes(q))
    : networkEntries;

  if (filtered.length === 0) {
    netSummary.textContent = networkEntries.length === 0
      ? "No requests captured yet."
      : "No matches.";
    netList.innerHTML = '<div class="empty-state">No requests captured yet.</div>';
    return;
  }

  netSummary.textContent = `${filtered.length} requests`;

  netList.innerHTML = filtered.map((e) => {
    const methodBadge = e.method
      ? `<span class="badge badge-method-${escapeHtml(e.method.toLowerCase())}">${escapeHtml(e.method)}</span>`
      : "";
    const statusBadge = e.status
      ? `<span class="badge ${statusBadgeClass(e.status)}">${e.status}</span>`
      : "";
    let shortUrl = e.url;
    try {
      const u = new URL(e.url);
      shortUrl = u.pathname + u.search;
    } catch (_) { /* keep full url */ }
    const timeStr = e.time != null ? `${Math.round(e.time)}ms` : "";
    const title = escapeHtml(e.url);
    return `<div class="net-row" onclick="window._copyNetUrl('${title}')">` +
      `${methodBadge}${statusBadge}` +
      `<span class="net-url" title="${title}">${escapeHtml(shortUrl)}</span>` +
      (timeStr ? `<span class="net-time">${timeStr}</span>` : "") +
      `</div>`;
  }).join("");
}

window._copyNetUrl = function (url) { copyText(url); };

function copyAllNetwork() {
  const lines = networkEntries.map((e) => {
    const status = e.status || "—";
    const method = e.method || "—";
    return `${status}\t${method}\t${e.url}\t${e.time != null ? Math.round(e.time) + "ms" : "—"}`;
  });
  copyText(lines.join("\n"));
}

function clearNetwork() {
  send({ type: "zerodom-clear-network-log" }).then(() => {
    networkEntries = [];
    applyNetworkFilter();
    toast("Cleared");
  });
}

netFilter.addEventListener("input", applyNetworkFilter);
$("#net-copy-all").addEventListener("click", copyAllNetwork);
$("#net-clear").addEventListener("click", clearNetwork);

async function refreshNetwork() {
  const res = await send({ type: "zerodom-get-network-log" });
  renderNetwork(Array.isArray(res) ? res : (res && res.entries) || []);
}

// ─── Cookies tab ─────────────────────────────────────────────────────────

function renderCookies(data) {
  if (!data || !data.cookies || data.cookies.length === 0) {
    cookieSummary.textContent = data && data.error
      ? `Error: ${data.error}`
      : "No cookies for this origin.";
    cookieList.innerHTML = '<div class="empty-state">No cookies for this origin.</div>';
    return;
  }

  const cookies = data.cookies;
  const httpOnlyCount = cookies.filter((c) => c.httpOnly).length;
  const secureCount = cookies.filter((c) => c.secure).length;
  cookieSummary.textContent = `${cookies.length} cookies — ${httpOnlyCount} httpOnly, ${secureCount} secure`;

  cookieList.innerHTML = cookies.map((c) => {
    const val = c.value.length > 30 ? c.value.slice(0, 30) + "…" : c.value;
    const flags = [];
    if (c.httpOnly) flags.push('<span class="flag flag-httponly">httpOnly</span>');
    if (c.secure) flags.push('<span class="flag flag-secure">secure</span>');
    if (c.sameSite && c.sameSite !== "None") {
      flags.push(`<span class="flag flag-samesite">${escapeHtml(c.sameSite)}</span>`);
    }
    return `<div class="cookie-row" onclick="window._copyCookieVal('${escapeHtml(c.value)}')">` +
      `<span class="cookie-name">${escapeHtml(c.name)}</span>` +
      `<span class="cookie-value" title="${escapeHtml(c.value)}">${escapeHtml(val)}</span>` +
      `<span class="cookie-domain">${escapeHtml(c.domain)}</span>` +
      `<span class="flags">${flags.join("")}</span>` +
      `</div>`;
  }).join("");
}

window._copyCookieVal = function (val) { copyText(val); };

function copyCookiesJson() {
  const pairs = {};
  const data = lastCookieData;
  if (data && data.cookies) {
    for (const c of data.cookies) {
      pairs[c.name] = c.value;
    }
  }
  copyText(JSON.stringify(pairs, null, 2));
}

function copyCookiesFull() {
  const data = lastCookieData;
  if (data && data.cookies) {
    copyText(JSON.stringify(data.cookies, null, 2));
  }
}

let lastCookieData = null;

$("#cookie-copy-json").addEventListener("click", copyCookiesJson);
$("#cookie-copy-full").addEventListener("click", copyCookiesFull);

async function refreshCookies() {
  const res = await send({ type: "zerodom-get-cookies" });
  lastCookieData = res;
  renderCookies(res);
}

// ─── Web storage tab (F11) ────────────────────────────────────────────────
// localStorage/sessionStorage read over the same CDP connection as cookies,
// with a lazy JWT payload decode on values that look like tokens (three
// dot-separated base64url segments) so an agent inspecting an auth flow can
// see what claim the session actually carries without leaving the popup.

function decodeJwtPayload(value) {
  // header.payload.signature — validate shape before touching the base64url.
  const parts = value.split(".");
  if (parts.length !== 3) return null;
  const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
  try {
    const json = atob(b64.padEnd(b64.length + ((4 - (b64.length % 4)) % 4), "="));
    const obj = JSON.parse(json);
    return typeof obj === "object" && obj !== null ? obj : null;
  } catch (e) {
    return null;
  }
}

function storageRow(k, v, src) {
  const jwt = decodeJwtPayload(v);
  const truncated = v.length > 140 ? v.slice(0, 140) + "…" : v;
  let claims = "";
  if (jwt) {
    const claimNames = ["sub", "role", "exp", "iat", "iss", "aud"];
    const bits = claimNames
      .filter((c) => jwt[c] !== undefined)
      .map((c) => `${c}=${typeof jwt[c] === "object" ? JSON.stringify(jwt[c]) : jwt[c]}`);
    claims = `<span class="flag flag-jwt" title="${escapeHtml(JSON.stringify(jwt, null, 2))}">JWT: ${escapeHtml(bits.join(" "))}</span>`;
  }
  return `<div class="cookie-row storage-row" style="align-items:flex-start;" title="${escapeHtml(v)}">` +
    `<span class="storage-src">${src === "L" ? "L" : "S"}</span>` +
    `<div style="min-width:0;flex:1;">` +
    `<div class="cookie-name" style="word-break:break-all;">${escapeHtml(k)}</div>` +
    `<div class="cookie-value" style="font-size:10px;white-space:normal;word-break:break-all;">${escapeHtml(truncated)}</div>` +
    `${claims ? `<div class="flags" style="margin-top:3px;">${claims}</div>` : ""}` +
    `</div></div>`;
}

function renderStorage(data) {
  const local = (data && data.local) || [];
  const session = (data && data.session) || [];
  const total = local.length + session.length;
  storageSummary.textContent = data && data.error
    ? `Error: ${data.error}`
    : `${total} keys — ${local.length} localStorage, ${session.length} sessionStorage`;
  if (total === 0) {
    storageList.innerHTML = '<div class="empty-state">No localStorage/sessionStorage.</div>';
    return;
  }
  storageList.innerHTML = [
    ...local.map((e) => storageRow(e.key, e.value, "L")),
    ...session.map((e) => storageRow(e.key, e.value, "S")),
  ].join("");
}

async function refreshStorage() {
  const res = await send({ type: "zerodom-get-storage" });
  renderStorage(res);
}

// ─── Tools tab ───────────────────────────────────────────────────────────

$("#tool-screenshot").addEventListener("click", async () => {
  const res = await send({ type: "zerodom-screenshot" });
  if (res && res.data) {
    chrome.tabs.create({ url: "data:image/png;base64," + res.data });
  } else {
    toast((res && res.error) || "No attached tab");
  }
});

$("#tool-copy-url").addEventListener("click", async () => {
  const res = await send({ type: "zerodom-get-page-url" });
  if (res && res.url) {
    copyText(res.url);
  } else {
    toast("No attached tab");
  }
});

$("#tool-open-new").addEventListener("click", async () => {
  const res = await send({ type: "zerodom-get-page-url" });
  if (res && res.url) {
    await send({ type: "zerodom-open-new-tab", url: res.url });
    toast("Opened");
  } else {
    toast("No attached tab");
  }
});

$("#tool-clear-net").addEventListener("click", clearNetwork);

// ─── Graph tab ───────────────────────────────────────────────────────────
// The Graph browser itself is graph_ui.js, shared verbatim with the in-page
// sidebar mcp_server.py injects. Everything below is just this host's half of
// that module's contract: in the popup, every page operation is a round-trip
// through background.js's chrome.debugger attachment.

const SNAPSHOT_KEY = (site) => `zerodom-snapshot:${site}`;

const graphUI = window.ZeroDOMGraphUI.create($("#graph-root"), {
  copy: copyText,
  toast,
  setHTML: (el, html) => { el.innerHTML = html; },
  listMaxHeight: "340px",

  highlight: (selector, clear) =>
    send({ type: "zerodom-highlight-node", selector, clear }),

  act: async (selector) => {
    const res = await send({ type: "zerodom-act-node", selector, action: "click" });
    return !!(res && res.ok);
  },

  // F6: one batched querySelectorAll pass for the whole graph, rather than a
  // round-trip per row — the popup pays CDP latency per message, so N messages
  // for N nodes would make a 200-node page unusable. Both this and drainClicks
  // are page round-trips, so they no-op while the tab is off screen; the
  // 2s auto-refresh below would otherwise keep paying for an invisible panel.
  validate: async (selectors) => {
    if (!graphPanelActive()) return null;
    const res = await send({ type: "zerodom-validate-selectors", selectors });
    return res && res.counts;
  },

  // Tree view: real DOM ancestry, batched into one page evaluate. Gated on the
  // panel being visible for the same reason as validate below.
  ancestry: async (selectors) => {
    if (!graphPanelActive()) return null;
    const res = await send({ type: "zerodom-get-ancestry", selectors });
    return res && res.paths ? { paths: res.paths, labels: res.labels } : null;
  },

  // F1: the capture listener is installed lazily (idempotent) and its queue
  // drained on every refresh, so a real click on the page flashes its row.
  drainClicks: async () => {
    if (!graphPanelActive()) return [];
    await send({ type: "zerodom-ensure-click-capture" });
    const res = await send({ type: "zerodom-drain-clicked" });
    return (res && res.ids) || [];
  },

  siteKey: () => {
    try { return new URL(currentUrl || "").hostname || "unknown"; } catch (e) { return "unknown"; }
  },
  snapshotGet: async (site) => (await chrome.storage.local.get(SNAPSHOT_KEY(site)))[SNAPSHOT_KEY(site)] || null,
  snapshotSet: (site, data) => chrome.storage.local.set({ [SNAPSHOT_KEY(site)]: data }),
  snapshotDel: (site) => chrome.storage.local.remove(SNAPSHOT_KEY(site)),
});

function graphPanelActive() {
  return $("#panel-graph").classList.contains("active");
}

async function refreshGraph() {
  const res = await send({ type: "zerodom-get-graph" });
  if (!res) return;
  await graphUI.update(res.nodes || [], { comp: res.compTokens || null, aria: res.ariaTokens || null });
}

// ─── Status listener ────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "zerodom-status") {
    currentStatus = message.status;
    renderSession(message.status, message.tabs);
    refreshLiveStats();
  }
});

// ─── Init ────────────────────────────────────────────────────────────────

(async function init() {
  const res = await send({ type: "zerodom-get-status" });
  if (res) {
    currentStatus = res.status;
    renderSession(res.status, res.tabs);
  }
  refreshLiveStats();
})();

// ─── Auto-refresh Network + Graph tabs while active ──────────────────────

let activeRefreshInterval = null;

function startActiveRefresh() {
  if (activeRefreshInterval) return;
  activeRefreshInterval = setInterval(() => {
    const activePanel = $("section.active");
    if (!activePanel) return;
    const id = activePanel.id;
    if (id === "panel-session") refreshLiveStats();
    if (id === "panel-network") refreshNetwork();
    if (id === "panel-cookies") refreshCookies();
    if (id === "panel-graph") refreshGraph();
  }, 2000);
}

startActiveRefresh();
