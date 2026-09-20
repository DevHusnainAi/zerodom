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
    if (btn.dataset.panel === "cookies") refreshCookies();
    if (btn.dataset.panel === "graph") refreshGraph();
  });
});

// ─── Session tab ────────────────────────────────────────────────────────

let currentStatus = "disconnected";

function renderSession(status, tabs) {
  statusText.textContent = status;
  dot.className = "dot " + status;
  if (status === "connected") {
    actionBtn.textContent = "Disconnect";
    actionBtn.className = "disconnect";
  } else if (status === "connecting") {
    actionBtn.textContent = "Connecting…";
    actionBtn.className = "";
  } else {
    actionBtn.textContent = "Connect";
    actionBtn.className = "";
  }
  renderTabs(tabs);
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
      ? `<span class="badge badge-method">${escapeHtml(e.method)}</span>`
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

async function refreshCookies() {
  const res = await send({ type: "zerodom-get-cookies" });
  lastCookieData = res;
  renderCookies(res);
}

// ─── Graph tab ───────────────────────────────────────────────────────────

let graphNodes = [];
let graphCompTokens = null;
let graphAriaTokens = null;
let graphPrevIds = new Set();
let graphActiveChips = new Set();
let graphViewMode = "grouped"; // "grouped" | "flat"

function getStability(selector) {
  if (!selector) return { level: "unknown", color: "gray", label: "?" };
  if (selector.startsWith("#")) return { level: "stable", color: "green", label: "#" };
  if (selector.includes(".")) return { level: "moderate", color: "yellow", label: "." };
  return { level: "fragile", color: "red", label: "structural" };
}

function estimateTokens(node) {
  return Math.ceil((node.label.length + node.type.length) / 4);
}

function inferGroup(type) {
  const t = (type || "").toLowerCase();
  if (["button", "input", "textbox", "textarea", "select", "combobox", "checkbox", "radio", "option", "slider", "spinbutton", "searchbox"].includes(t)) {
    return "Form Controls";
  }
  if (["link", "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "treeitem"].includes(t)) {
    return "Navigation";
  }
  if (["heading", "img", "separator", "figure", "group", "list", "listitem", "table", "row", "cell"].includes(t)) {
    return "Content";
  }
  return "Other";
}

function renderGraph() {
  const graphSummary = $("#graph-summary");
  const graphChips = $("#graph-chips");
  const graphBody = $("#graph-body");
  if (!graphBody) return;

  const totalTokens = graphNodes.reduce((s, n) => s + estimateTokens(n), 0);

  const compStr = graphCompTokens
    ? `comp: ${graphCompTokens.before} → ${graphCompTokens.after}`
    : "";
  const ariaStr = graphAriaTokens
    ? `aria: ${graphAriaTokens.before} → ${graphAriaTokens.after}`
    : "";
  const tokenParts = [compStr, ariaStr].filter(Boolean).join(" · ");

  graphSummary.textContent =
    `${graphNodes.length} nodes · ${totalTokens} tokens` +
    (tokenParts ? ` · ${tokenParts}` : "");

  // Build chip set from unique types
  const types = [...new Set(graphNodes.map((n) => n.type))].sort();
  graphChips.innerHTML = types.map((t) => {
    const active = graphActiveChips.has(t) ? " active" : "";
    const count = graphNodes.filter((n) => n.type === t).length;
    return `<button class="chip${active}" data-type="${escapeHtml(t)}">${escapeHtml(t)} (${count})</button>`;
  }).join("");

  $$(".chip", graphChips).forEach((chip) => {
    chip.addEventListener("click", () => {
      const t = chip.dataset.type;
      if (graphActiveChips.has(t)) {
        graphActiveChips.delete(t);
      } else {
        graphActiveChips.add(t);
      }
      renderGraph();
    });
  });

  // Filter nodes
  const textFilter = ($("#graph-filter") ? $("#graph-filter").value : "").toLowerCase();
  let filtered = graphNodes;
  if (graphActiveChips.size > 0) {
    filtered = filtered.filter((n) => graphActiveChips.has(n.type));
  }
  if (textFilter) {
    filtered = filtered.filter((n) =>
      (n.label || "").toLowerCase().includes(textFilter) ||
      (n.type || "").toLowerCase().includes(textFilter)
    );
  }

  // Diff detection
  const currentIds = new Set(graphNodes.map((n) => n.id));

  if (graphViewMode === "grouped") {
    const groups = {};
    for (const n of filtered) {
      const g = inferGroup(n.type);
      if (!groups[g]) groups[g] = [];
      groups[g].push(n);
    }

    graphBody.innerHTML = Object.entries(groups).map(([name, nodes]) => {
      const groupTokens = nodes.reduce((s, n) => s + estimateTokens(n), 0);
      const header = `<div class="graph-container-header">` +
        `<span class="graph-container-name">${escapeHtml(name)}</span>` +
        `<span class="graph-container-count">${nodes.length} nodes</span>` +
        `<span class="graph-container-tokens">${groupTokens} tokens</span>` +
        `</div>`;
      const rows = nodes.map((n) => graphNodeRow(n, currentIds)).join("");
      return `<div class="graph-container">${header}<div class="graph-container-body">${rows}</div></div>`;
    }).join("");
  } else {
    graphBody.innerHTML = filtered.map((n) => graphNodeRow(n, currentIds)).join("");
  }

  // Attach click handlers for copy-selector
  $$(".graph-node-row", graphBody).forEach((el) => {
    el.addEventListener("click", () => {
      const sel = el.dataset.selector;
      if (sel) {
        copyText(sel);
      }
    });
  });
}

function graphNodeRow(node, currentIds) {
  const stability = getStability(node.selector);
  const tokens = estimateTokens(node);
  let diffClass = "";
  if (!graphPrevIds.has(node.id)) diffClass = " diff-added";
  const stabDot = `<span class="stab-dot stab-${stability.color}" title="${stability.label}"></span>`;
  const typeBadge = `<span class="badge badge-type">${escapeHtml(node.type)}</span>`;
  const tokenBadge = `<span class="badge badge-tokens">${tokens}tok</span>`;
  const label = escapeHtml(node.label || "(no label)");
  const title = node.selector ? `title="${escapeHtml(node.selector)} — click to copy"` : "";
  return `<div class="graph-node-row${diffClass}" data-selector="${escapeHtml(node.selector || "")}" ${title}>` +
    `${stabDot}${typeBadge}<span class="graph-node-label">${label}</span>${tokenBadge}` +
    `</div>`;
}

// Graph view toggle
document.addEventListener("click", (e) => {
  if (e.target.matches(".graph-view-btn")) {
    graphViewMode = e.target.dataset.view;
    $$(".graph-view-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === graphViewMode));
    renderGraph();
  }
});

// Graph text filter
document.addEventListener("input", (e) => {
  if (e.target.matches("#graph-filter")) {
    renderGraph();
  }
});

async function refreshGraph() {
  const res = await send({ type: "zerodom-get-graph" });
  if (!res) return;

  graphPrevIds = new Set(graphNodes.map((n) => n.id));
  graphNodes = res.nodes || [];
  graphCompTokens = res.compTokens || null;
  graphAriaTokens = res.ariaTokens || null;
  renderGraph();
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
