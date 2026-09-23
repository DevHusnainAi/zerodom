// ─── ZeroDOM graph UI ─────────────────────────────────────────────────────
// The Graph browser, rendered identically in two places: the extension
// popup's Graph tab and the in-page sidebar mcp_server.py injects into a
// driven tab. Canonical copy lives in zerodom/ (so it ships in the wheel);
// extension/graph_ui.js is a byte-identical mirror that tests/test_graph_ui_sync.py
// enforces — edit this file, then `cp zerodom/graph_ui.js extension/graph_ui.js`.
// (Two copies because a wheel can't ship extension/ and an MV3 popup can't
// load a file from outside its own directory. The test is the guard.)
//
// Everything host-specific is injected rather than assumed, because the two
// hosts have nothing in common below the render layer: the popup reaches the
// page through chrome.debugger round-trips, the sidebar is already *in* the
// page and can just touch the DOM. See HOST below for the contract.
//
// Deliberately a classic script assigning one global, not an ES module: the
// popup loads it with <script src>, and the sidebar gets it as an evaluated
// string over CDP, where `import` has no meaning.

(function () {
  "use strict";

  // HOST CONTRACT — every field required unless marked optional.
  //   copy(text)              copy to clipboard and toast the result
  //   toast(msg)              transient status message
  //   setHTML(el, html)       assign markup (Trusted-Types-safe in the page)
  //   highlight(sel, clear)   async; outline the matching element on the page
  //   act(sel)                async -> truthy if the click landed
  //   validate(selectors)     async -> {selector: liveMatchCount}; optional
  //   drainClicks()           async -> [nodeId]; optional (F1 reverse select)
  //   ancestry(selectors)     async -> {paths:{sel:[key,...]}, labels:{key:str}}
  //                           optional; enables the Tree view. Keys identify DOM
  //                           ancestors root-first; without it the Tree button is
  //                           hidden rather than rendering a lie.
  //   siteKey()               string key snapshots are stored under
  //   snapshotGet(key)        async -> {nodes:[...]} | null
  //   snapshotSet(key, data)  async
  //   snapshotDel(key)        async
  //   listMaxHeight           optional CSS length for the scroll area

  const MARKUP =
    '<div class="summary" data-zd="summary">—</div>' +
    '<input class="filter-input" data-zd="filter" type="text" placeholder="Filter by label…">' +
    '<div class="chip-bar" data-zd="chips"></div>' +
    '<div class="view-toggle" data-zd="views">' +
      '<button class="view-btn active" data-view="grouped">Grouped</button>' +
      '<button class="view-btn" data-view="flat">Flat</button>' +
      '<button class="view-btn" data-view="tree" data-zd="view-tree" hidden>Tree</button>' +
    '</div>' +
    '<div class="action-bar" style="margin-bottom:10px;">' +
      '<button class="action-btn" data-zd="snap-save">Save Snapshot</button>' +
      '<button class="action-btn" data-zd="snap-compare">Compare</button>' +
      '<button class="action-btn" data-zd="snap-clear" title="Clear stored snapshot">Clear</button>' +
      '<span class="snapshot-status" data-zd="snap-status"></span>' +
    '</div>' +
    '<div class="bulk-bar" data-zd="bulk-bar" style="display:none;">' +
      '<span class="bulk-count" data-zd="bulk-count">0 selected</span>' +
      '<div class="bulk-actions">' +
        '<button class="action-btn" data-zd="bulk-ids">Copy IDs</button>' +
        '<button class="action-btn" data-zd="bulk-selectors">Copy Selectors</button>' +
        '<button class="action-btn" data-zd="bulk-clear">Clear</button>' +
      '</div>' +
    '</div>' +
    '<div class="scroll-list" data-zd="list"><div class="empty-state">No graph data yet.</div></div>' +
    '<div class="code-panel" data-zd="code-panel" style="display:none;"></div>' +
    '<div class="ctx-menu" data-zd="ctx-menu"></div>';

  // F13: security-relevant node classification. Pure client-side logic over the
  // fields shipped in the sidebar payload (input_type/content_editable/
  // disabled/required) — no page round-trip. Mirrors the server-side
  // _is_sensitive_field() regex in mcp_server.py so a human sees the same
  // fields the model is warned about.
  const SENSITIVE_RE = /passw|passwd|\bpwd\b|card ?number|card ?no\b|\bcvv\b|\bcvc\b|security code|\bssn\b|social security|routing number|account number|\biban\b|sort code/i;

  function getStability(selector) {
    if (!selector) return { level: "unknown", label: "?" };
    if (selector.startsWith("#")) return { level: "stable", label: "#" };
    if (selector.includes(".")) return { level: "moderate", label: "." };
    return { level: "fragile", label: "structural" };
  }

  function estimateTokens(node) {
    return Math.ceil(((node.label || "").length + (node.type || "").length) / 4);
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

  function severityOf(node) {
    const label = (node.label || "") + " " + (node.placeholder || "");
    const href = (node.href || "").replace(/^[\s\x00-\x1f]+/, "").toLowerCase();
    let level = null;
    let tag = null;
    // F12: XSS / injection surface flags (partial — innerHTML/eval sinks need
    // page instrumentation; these are the surfaces detectable from the graph node
    // alone). Highest-severity first: password/file take precedence only because
    // they're the action the user *intended* to protect, while these are the
    // actions an attacker might want to steal or abuse.
    if (href.startsWith("javascript:")) { level = "critical"; tag = "js: href"; }
    else if (href.startsWith("vbscript:")) { level = "critical"; tag = "vbscript: href"; }
    else if (href.startsWith("data:text/html")) { level = "high"; tag = "data:html"; }
    else if (href.startsWith("data:")) { level = "high"; tag = "data: href"; }
    else if (node.content_editable) { level = "high"; tag = "editable"; }
    else if (node.input_type === "password") { level = "critical"; tag = "password"; }
    else if (node.input_type === "file") { level = "high"; tag = "file"; }
    else if (node.input_type === "url") { level = "medium"; tag = "url input"; }
    else if (/\b(html|svg)\b/i.test(node.placeholder || "")) { level = "medium"; tag = "html placeholder"; }
    if (SENSITIVE_RE.test(label)) { level = "medium"; tag = "sensitive"; }
    if (node.disabled) { level = "medium"; tag = "disabled"; }
    if (node.required) { level = "medium"; tag = "required"; }
    return level ? { level, tag } : null;
  }

  function isDestructive(node) {
    return /delete|remove|destroy|reset|teardown/i.test(node.label || "") ||
      /delete|remove/i.test(node.type || "");
  }

  function codeSnippets(node) {
    const sel = (node.selector || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
    return [
      { name: "ZeroDOM", code: "[" + node.id + "]" },
      { name: "Playwright", code: "await page.locator('" + sel + "').click();" },
      { name: "Cypress", code: "cy.get('" + sel + "').click();" },
      { name: "Selenium", code: "driver.find_element(By.CSS_SELECTOR, '" + sel + "').click()" },
    ];
  }

  function copyFormats(node) {
    const sel = (node.selector || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
    return [
      { label: "[" + node.id + "]", hint: "ZeroDOM id", text: "[" + node.id + "]" },
      { label: "page.locator('" + sel + "')", hint: "Playwright", text: "page.locator('" + sel + "')" },
      { label: "cy.get('" + sel + "')", hint: "Cypress", text: "cy.get('" + sel + "')" },
      { label: "driver.find_element(By.CSS_SELECTOR, '" + sel + "')", hint: "Selenium", text: "driver.find_element(By.CSS_SELECTOR, '" + sel + "')" },
      { label: node.selector, hint: "raw selector", text: node.selector },
    ];
  }

  // Build a nesting tree from per-node ancestor key paths.
  //
  // Showing every DOM ancestor would bury the graph: on Hacker News a single
  // link sits under table>tbody>tr>td>table>tbody>tr>td, none of which mean
  // anything on their own. So a container earns a row only where it actually
  // *branches* — where it holds more than one distinct child in the tree.
  // Single-child chains collapse into their child, which turns that HN chain
  // into one "story row" node holding its upvote/title/user/comments links:
  // the structure a person is actually looking for.
  function buildTree(nodes, paths) {
    const root = { key: "__root", children: new Map(), nodes: [] };
    for (const n of nodes) {
      const path = (paths && paths[n.selector]) || [];
      let cur = root;
      for (const key of path) {
        if (!cur.children.has(key)) cur.children.set(key, { key, children: new Map(), nodes: [] });
        cur = cur.children.get(key);
      }
      cur.nodes.push(n);
    }
    // A container earns a row only where it is a real branch point — where it
    // contributes two or more entries (leaves or sub-branches). Anything with
    // exactly one passes its entry straight up, so the table>tbody>tr>td>a
    // chains that plain HTML is full of collapse to nothing and what survives
    // is the structure a person means by "which controls belong together".
    function shape(n) {
      let entries = n.nodes.map((g) => ({ leaf: g }));
      for (const child of n.children.values()) entries = entries.concat(shape(child));
      if (entries.length <= 1) return entries;
      return [{ branch: n.key, entries }];
    }
    return shape(root);
  }

  function create(root, host) {
    const doc = root.ownerDocument || document;
    const wrap = doc.createElement("div");
    wrap.className = "zd-graph-root";
    host.setHTML(wrap, MARKUP);
    root.appendChild(wrap);

    const $ = (name) => wrap.querySelector('[data-zd="' + name + '"]');
    const $$ = (sel, scope) => Array.prototype.slice.call((scope || wrap).querySelectorAll(sel));

    function escapeHtml(s) {
      const div = doc.createElement("div");
      div.textContent = s == null ? "" : String(s);
      return div.innerHTML;
    }

    const listEl = $("list");
    if (host.listMaxHeight) listEl.style.maxHeight = host.listMaxHeight;

    let graphNodes = [];
    let graphCompTokens = null;
    let graphAriaTokens = null;
    let graphPrevIds = new Set();
    // The first update has no previous graph to diff against, so every node
    // would read as "just appeared" and the whole list renders ringed green.
    // The popup hid this by refreshing every 2s; the sidebar only updates when
    // the agent acts, so a first-paint false positive can sit there for minutes
    // -- and drowns out any other state the row is trying to show.
    let hasPriorGraph = false;
    let graphSnapshotNodes = null; // F9: saved snapshot for node comparison
    let graphActiveChips = new Set();
    let graphActiveRoleChips = new Set();
    let graphActiveFlags = new Set();
    let graphViewMode = "grouped"; // "grouped" | "flat" | "tree"
    let ancestry = null; // {paths, labels} from host.ancestry, refreshed per update
    let selectedNodeIds = new Set();
    let lastSelectedIndex = null;
    let selectorCounts = {}; // selector -> live match count (F6, from host.validate)

    function selectedNodes() {
      const byId = new Map(graphNodes.map((n) => [n.id, n]));
      return Array.from(selectedNodeIds).map((id) => byId.get(id)).filter(Boolean);
    }

    function graphNodeRow(node) {
      const stability = getStability(node.selector);
      const count = node.selector ? selectorCounts[node.selector] : undefined;
      // F6: when a validation pass has run, the dot reflects the *live* match
      // count (green=1, amber=N, red=0/invalid) instead of the predictive
      // pattern guess — the empirical answer replaces, not supplements, the guess.
      const lv = count === undefined
        ? stability.level
        : count === 1 ? "stable" : count > 1 ? "moderate" : "fragile";
      const dotLabel = count === undefined
        ? stability.label
        : count < 0 ? "invalid selector" : count === 0 ? "no match" : count === 1 ? "unique match" : count + " matches";
      const tokens = estimateTokens(node);
      let diffClass = "";
      if (hasPriorGraph && !graphPrevIds.has(node.id)) diffClass = " diff-added";
      let diffBadge = "";
      // F9: when a snapshot is loaded, each row shows its delta vs the snapshot
      // (added / changed / removed) instead of (or beside) the bare-added marker.
      if (graphSnapshotNodes) {
        const snapMap = new Map(graphSnapshotNodes.map((n) => [n.id, n]));
        const snap = snapMap.get(node.id);
        if (!snap) {
          diffClass = " diff-added";
          diffBadge = '<span class="diff-badge diff-b-new" title="not in snapshot">+new</span>';
        } else if (snap.label !== node.label || snap.type !== node.type) {
          diffClass = " diff-changed";
          diffBadge = '<span class="diff-badge diff-b-chan" title="label/type changed">~chg</span>';
        }
      }
      const selected = selectedNodeIds.has(node.id) ? " selected" : "";
      const stabDot = '<span class="stability stability-' + lv + '" title="' + escapeHtml(dotLabel) + '"></span>';
      const typeBadge = '<span class="graph-node-type">' + escapeHtml(node.type) + "</span>";
      const tokenBadge = '<span class="token-badge">' + tokens + "tok</span>";
      const sev = severityOf(node);
      const sevBadge = sev
        ? '<span class="sev-badge sev-' + sev.level + '" title="security: ' + escapeHtml(sev.tag) + '">' + escapeHtml(sev.tag[0]) + "</span>"
        : "";
      const sevClass = sev ? " sev-" + sev.level : "";
      const label = escapeHtml(node.label || "(no label)");
      const title = node.selector
        ? 'title="' + escapeHtml(node.selector + " — " + dotLabel + " · right-click for copy options · click to select, Shift+click range, Ctrl/Cmd+click toggle, double-click to act") + '"'
        : "";
      return '<div class="graph-node' + diffClass + sevClass + selected + '" data-selector="' + escapeHtml(node.selector || "") + '" data-node-id="' + escapeHtml(node.id) + '" ' + title + ">" +
        stabDot + sevBadge + diffBadge + typeBadge + '<span class="graph-node-label">' + label + "</span>" + tokenBadge +
        '<span class="row-actions">' +
        '<button class="row-act row-copy" title="Copy selector">⧉</button>' +
        '<button class="row-act row-act-btn" title="Click on page">▶</button>' +
        "</span>" +
        "</div>";
    }

    function renderEntries(entries, depth) {
      let out = "";
      for (const e of entries) {
        if (e.leaf) {
          out += '<div class="tree-leaf" style="--d:' + depth + '">' + graphNodeRow(e.leaf) + "</div>";
          continue;
        }
        const label = (ancestry.labels && ancestry.labels[e.branch]) || e.branch;
        const count = countLeaves(e.entries);
        out += '<div class="tree-branch" style="--d:' + depth + '">' +
          '<span class="tree-twig"></span>' +
          '<span class="tree-label">' + escapeHtml(label) + "</span>" +
          '<span class="graph-container-count">' + count + (count === 1 ? " node" : " nodes") + "</span>" +
          "</div>";
        out += renderEntries(e.entries, depth + 1);
      }
      return out;
    }

    function countLeaves(entries) {
      return entries.reduce((s, e) => s + (e.leaf ? 1 : countLeaves(e.entries)), 0);
    }

    function render() {
      const totalTokens = graphNodes.reduce((s, n) => s + estimateTokens(n), 0);

      const compStr = graphCompTokens
        ? "comp: " + graphCompTokens.before + " → " + graphCompTokens.after
        : "";
      const ariaStr = graphAriaTokens
        ? "aria: " + graphAriaTokens.before + " → " + graphAriaTokens.after
        : "";
      const tokenParts = [compStr, ariaStr].filter(Boolean).join(" · ");

      $("summary").textContent =
        graphNodes.length + " nodes · " + totalTokens + " tokens" +
        (tokenParts ? " · " + tokenParts : "");

      // Build chip set from unique types
      const types = [...new Set(graphNodes.map((n) => n.type))].sort();
      const typeChipsHtml = types.map((t) => {
        const active = graphActiveChips.has(t) ? " active" : "";
        const count = graphNodes.filter((n) => n.type === t).length;
        return '<button class="chip' + active + '" data-type="' + escapeHtml(t) + '">' + escapeHtml(t) + " (" + count + ")</button>";
      }).join("");

      // Flag chips
      const flagDefs = [
        { key: "hidden", label: "hidden" },
        { key: "disabled", label: "disabled" },
        { key: "destructive", label: "destructive" },
      ];
      const flagChipsHtml = flagDefs.map((f) => {
        const count = graphNodes.filter((n) => {
          return f.key === "hidden" ? !!n.hidden : f.key === "disabled" ? !!n.disabled : isDestructive(n);
        }).length;
        if (count === 0) return "";
        const active = graphActiveFlags.has(f.key) ? " active" : "";
        return '<button class="chip' + active + '" data-flag="' + f.key + '">' + f.label + " (" + count + ")</button>";
      }).join("");

      // Role filter chips (F3) — semantic role bar distinct from the raw-tag type
      // bar above: a <div role="button"> filters under "button", an ARIA tab under
      // "tab", matching how an agent thinks about the page rather than the HTML.
      const roles = [...new Set(graphNodes.map((n) => n.role).filter(Boolean))].sort();
      const roleChipsHtml = roles.length
        ? '<span class="chip-bar-sep"></span>' + roles.map((r) => {
            const active = graphActiveRoleChips.has(r) ? " active" : "";
            const count = graphNodes.filter((n) => n.role === r).length;
            return '<button class="chip' + active + '" data-role="' + escapeHtml(r) + '">' + escapeHtml(r) + " (" + count + ")</button>";
          }).join("")
        : "";

      host.setHTML($("chips"), typeChipsHtml + flagChipsHtml + roleChipsHtml);

      $$(".chip", $("chips")).forEach((chip) => {
        chip.addEventListener("click", () => {
          const typeAttr = chip.dataset.type;
          const roleAttr = chip.dataset.role;
          const flagAttr = chip.dataset.flag;
          const toggle = (set, key) => { if (set.has(key)) set.delete(key); else set.add(key); };
          if (typeAttr) toggle(graphActiveChips, typeAttr);
          else if (roleAttr) toggle(graphActiveRoleChips, roleAttr);
          else if (flagAttr) toggle(graphActiveFlags, flagAttr);
          render();
        });
      });

      // Filter nodes
      const textFilter = ($("filter") ? $("filter").value : "").toLowerCase();
      let filtered = graphNodes;
      if (graphActiveChips.size > 0) {
        filtered = filtered.filter((n) => graphActiveChips.has(n.type));
      }
      if (graphActiveRoleChips.size > 0) {
        filtered = filtered.filter((n) => graphActiveRoleChips.has(n.role));
      }
      if (graphActiveFlags.size > 0) {
        filtered = filtered.filter((n) => {
          const flagMap = { hidden: !!n.hidden, disabled: !!n.disabled, destructive: isDestructive(n) };
          return Array.from(graphActiveFlags).some((f) => flagMap[f]);
        });
      }
      if (textFilter) {
        filtered = filtered.filter((n) =>
          (n.label || "").toLowerCase().includes(textFilter) ||
          (n.type || "").toLowerCase().includes(textFilter) ||
          (n.role || "").toLowerCase().includes(textFilter) ||
          (n.placeholder || "").toLowerCase().includes(textFilter) ||
          (n.selector || "").toLowerCase().includes(textFilter) ||
          (n.card || "").toLowerCase().includes(textFilter)
        );
      }

      if (!filtered.length) {
        host.setHTML(listEl, '<div class="empty-state">' +
          (graphNodes.length ? "No nodes match this filter." : "No graph data yet.") + "</div>");
        renderBulkBar();
        renderCodePanel();
        return;
      }

      if (graphViewMode === "grouped") {
        const groups = {};
        for (const n of filtered) {
          const g = inferGroup(n.type);
          if (!groups[g]) groups[g] = [];
          groups[g].push(n);
        }

        host.setHTML(listEl, Object.entries(groups).map(([name, nodes]) => {
          const groupTokens = nodes.reduce((s, n) => s + estimateTokens(n), 0);
          const header = '<div class="graph-container-header">' +
            '<span class="graph-container-name">' + escapeHtml(name) + "</span>" +
            '<span class="graph-container-count">' + nodes.length + " nodes</span>" +
            '<span class="graph-container-tokens">' + groupTokens + " tokens</span>" +
            "</div>";
          const rows = nodes.map((n) => graphNodeRow(n)).join("");
          return '<div class="graph-container">' + header + '<div class="graph-container-body">' + rows + "</div></div>";
        }).join(""));
      } else if (graphViewMode === "tree" && ancestry) {
        const keep = new Set(filtered.map((n) => n.id));
        const entries = buildTree(graphNodes.filter((n) => keep.has(n.id)), ancestry.paths);
        host.setHTML(listEl, renderEntries(entries, 0));
      } else {
        host.setHTML(listEl, filtered.map((n) => graphNodeRow(n)).join(""));
      }

      // Attach click / hover handlers for copy-selector and highlight.
      // Selection model: plain click selects + copies the ZeroDOM id (the popup's
      // classic behavior); Shift+click extends a range from the last click;
      // Ctrl/Cmd+click toggles. Playwright/Cypress snippets live in the
      // right-click menu (F2) — modifier-click copying is gone, the menu is
      // discoverable and covers Selenium too.
      $$(".graph-node", listEl).forEach((el) => {
        el.addEventListener("click", (e) => {
          const sel = el.dataset.selector;
          const nodeId = el.dataset.nodeId;
          const idx = graphNodes.findIndex((n) => n.id === nodeId);
          if (idx < 0) return;

          if (e.shiftKey) {
            const anchor = lastSelectedIndex !== null ? lastSelectedIndex : idx;
            const lo = Math.min(anchor, idx);
            const hi = Math.max(anchor, idx);
            selectedNodeIds = new Set(graphNodes.slice(lo, hi + 1).map((n) => n.id));
          } else if (e.ctrlKey || e.metaKey) {
            if (selectedNodeIds.has(nodeId)) selectedNodeIds.delete(nodeId);
            else selectedNodeIds.add(nodeId);
          } else {
            selectedNodeIds = new Set([nodeId]);
            if (sel) host.copy("[" + nodeId + "]");
          }
          // The re-render below replaces the row under the cursor, so the
          // mouseleave that normally clears the page-side highlight never fires —
          // clear it explicitly.
          host.highlight("", true);
          lastSelectedIndex = idx;
          render();
        });
        el.addEventListener("dblclick", async () => {
          const sel = el.dataset.selector;
          if (!sel) return;
          const ok = await host.act(sel);
          host.toast(ok ? "Clicked" : "Act failed");
        });
        el.addEventListener("mouseenter", () => {
          const sel = el.dataset.selector;
          if (sel) host.highlight(sel, false);
        });
        el.addEventListener("mouseleave", () => {
          host.highlight("", true);
        });
        el.addEventListener("contextmenu", (e) => {
          e.preventDefault();
          const node = graphNodes.find((n) => n.id === el.dataset.nodeId);
          if (node) showContextMenu(e.clientX, e.clientY, node);
        });
        // F5: inline hover actions. The row already copies the id on plain click
        // and acts on double-click — these are the same two verbs surfaced as
        // visible buttons that appear on hover, for humans who don't know the
        // gestures. stopPropagation so the row's own click/select logic doesn't
        // also fire.
        const rowCopy = el.querySelector(".row-copy");
        if (rowCopy) {
          rowCopy.addEventListener("click", (e) => {
            e.stopPropagation();
            if (el.dataset.selector) host.copy(el.dataset.selector);
          });
        }
        const rowAct = el.querySelector(".row-act-btn");
        if (rowAct) {
          rowAct.addEventListener("click", async (e) => {
            e.stopPropagation();
            const sel = el.dataset.selector;
            if (!sel) return;
            const ok = await host.act(sel);
            host.toast(ok ? "Clicked" : "Act failed");
          });
        }
      });

      renderBulkBar();
      renderCodePanel();
    }

    // ─── Bulk selection (F8) ───────────────────────────────────────────────
    // Shift/ctrl-click on a row sets selectedNodeIds; the bar above the list
    // exposes actions that only make sense with N>1. Selection is deliberately
    // not persisted — the graph re-reads on every refresh and ids are cheap
    // to re-grab.

    function renderBulkBar() {
      const bar = $("bulk-bar");
      const sel = selectedNodes();
      if (sel.length < 2) {
        bar.style.display = "none";
        return;
      }
      bar.style.display = "flex";
      $("bulk-count").textContent = sel.length + " selected";
    }

    $("bulk-ids").addEventListener("click", () => {
      host.copy(selectedNodes().map((n) => "[" + n.id + "]").join(" "));
    });
    $("bulk-selectors").addEventListener("click", () => {
      host.copy(selectedNodes().map((n) => n.selector).join("\n"));
    });
    $("bulk-clear").addEventListener("click", () => {
      selectedNodeIds = new Set();
      lastSelectedIndex = null;
      render();
    });

    // ─── Snapshot / compare (F9) ───────────────────────────────────────────
    // Persist the current graph as a snapshot, then diff the live graph against
    // it. Each row gains a +new / ~chg badge; the status line says whether the
    // live graph has anything the snapshot doesn't. Origin-tagged key so
    // per-site snapshots don't collide. Where that snapshot actually lives is
    // the host's business — durable extension storage in the popup, a page
    // variable in the sidebar.

    function setSnapshotStatus(text) {
      $("snap-status").textContent = text;
    }

    function diffAgainst(snapNodes) {
      const snapSet = new Set(snapNodes.map((n) => n.id));
      const liveSet = new Set(graphNodes.map((n) => n.id));
      const added = graphNodes.filter((n) => !snapSet.has(n.id)).length;
      const removed = snapNodes.filter((n) => !liveSet.has(n.id)).length;
      const changed = graphNodes.filter((n) => snapSet.has(n.id)).filter((n) => {
        const s = snapNodes.find((x) => x.id === n.id);
        return s && (s.label !== n.label || s.type !== n.type);
      }).length;
      return added + " new, " + removed + " gone, " + changed + " changed";
    }

    $("snap-save").addEventListener("click", async () => {
      if (!graphNodes.length) { setSnapshotStatus("no graph"); return; }
      const snap = graphNodes.map((n) => ({ id: n.id, label: n.label, type: n.type, selector: n.selector }));
      await host.snapshotSet(host.siteKey(), { ts: Date.now(), nodes: snap });
      graphSnapshotNodes = snap;
      setSnapshotStatus("snapshot: " + snap.length + " nodes");
      render();
    });

    $("snap-compare").addEventListener("click", async () => {
      const stored = await host.snapshotGet(host.siteKey());
      if (!stored || !stored.nodes) { setSnapshotStatus("no snapshot saved"); return; }
      graphSnapshotNodes = stored.nodes;
      setSnapshotStatus("vs snapshot: " + diffAgainst(stored.nodes));
      render();
    });

    $("snap-clear").addEventListener("click", async () => {
      await host.snapshotDel(host.siteKey());
      graphSnapshotNodes = null;
      setSnapshotStatus("");
      render();
    });

    // ─── Code generation panel (F7) ────────────────────────────────────────
    // Bottom panel shown when exactly one node is selected: ready-to-paste
    // snippets for the major frameworks, generated client-side from the node's
    // selector — no server call involved.

    function renderCodePanel() {
      const panel = $("code-panel");
      const sel = selectedNodes();
      if (sel.length !== 1) {
        panel.style.display = "none";
        host.setHTML(panel, "");
        return;
      }
      const node = sel[0];
      const snippets = codeSnippets(node);
      panel.style.display = "block";
      const nodeLabel = escapeHtml((node.label || node.id) + " — " + node.type);
      const tabs = snippets.map((s, i) =>
        '<button class="code-tab' + (i === 0 ? " active" : "") + '" data-code-index="' + i + '">' + escapeHtml(s.name) + "</button>"
      ).join("");
      host.setHTML(panel,
        '<div class="code-panel-title"><span class="code-node-label">' + nodeLabel + "</span></div>" +
        '<div class="code-tabs">' + tabs + "</div>" +
        '<div class="code-block">' + escapeHtml(snippets[0].code) +
        '<button class="code-copy-btn">Copy</button></div>');

      $$(".code-tab", panel).forEach((tab) => {
        tab.addEventListener("click", () => {
          $$(".code-tab", panel).forEach((t) => t.classList.remove("active"));
          tab.classList.add("active");
          const code = snippets[parseInt(tab.dataset.codeIndex, 10)].code;
          panel.querySelector(".code-block").childNodes[0].nodeValue = code;
        });
      });
      panel.querySelector(".code-copy-btn").addEventListener("click", () => {
        const idx = panel.querySelector(".code-tab.active").dataset.codeIndex;
        host.copy(snippets[parseInt(idx, 10)].code);
      });
    }

    // ─── Context menu (F2) ─────────────────────────────────────────────────
    // Custom HTML menu, not chrome.contextMenus: the API's items show on every
    // right-click page-wide, not on specific rows. The copy targets below are
    // the same ones click/shift-click/ctrl-click already produce — this just
    // makes them visible instead of hiding them behind modifier keys.

    function showContextMenu(x, y, node) {
      const menu = $("ctx-menu");
      const items = copyFormats(node);
      host.setHTML(menu, items.map((it) =>
        '<button class="ctx-item" data-copy="' + escapeHtml(it.hint) + '">' +
        escapeHtml(it.label) + '<span class="ctx-k">' + escapeHtml(it.hint) + "</span>" +
        "</button>").join(""));

      menu.style.display = "block";
      menu.style.left = x + "px";
      menu.style.top = y + "px";
      menu.dataset.nodeId = node.id;

      $$(".ctx-item", menu).forEach((btn) => {
        btn.addEventListener("click", () => {
          const again = graphNodes.find((n) => n.id === menu.dataset.nodeId);
          if (!again) return;
          const item = copyFormats(again).find((f) => f.hint === btn.dataset.copy);
          if (item) host.copy(item.text);
          hideContextMenu();
        });
      });
    }

    function hideContextMenu() {
      const menu = $("ctx-menu");
      if (!menu) return;
      menu.style.display = "none";
      host.setHTML(menu, "");
    }

    doc.addEventListener("click", hideContextMenu);
    doc.addEventListener("wheel", hideContextMenu, { passive: true });
    doc.addEventListener("keydown", (e) => {
      if (e.key === "Escape") hideContextMenu();
    });

    $("views").addEventListener("click", (e) => {
      const btn = e.target.closest(".view-btn");
      if (!btn) return;
      graphViewMode = btn.dataset.view;
      $$(".view-btn", $("views")).forEach((b) => b.classList.toggle("active", b.dataset.view === graphViewMode));
      if (graphViewMode === "tree" && host.ancestry && !ancestry) {
        const selectors = [...new Set(graphNodes.map((n) => n.selector).filter(Boolean))];
        Promise.resolve(host.ancestry(selectors)).then((a) => { ancestry = a; render(); });
        return;
      }
      render();
    });

    $("filter").addEventListener("input", render);

    // F1: flash the rows whose node ids match real page clicks — clicking a
    // button on the page highlights its graph row, so a human sees exactly
    // which node an agent's `[03]` refers to.
    function flashRows(ids) {
      const set = new Set(ids.map((id) => String(id).replace(/^node_/, "")));
      $$(".graph-node", listEl).forEach((rowEl) => {
        const rid = (rowEl.dataset.nodeId || "").replace(/^node_/, "");
        if (set.has(rid)) {
          rowEl.classList.add("picked-flash");
          setTimeout(() => rowEl.classList.remove("picked-flash"), 900);
        }
      });
    }

    async function update(nodes, tokens) {
      graphPrevIds = new Set(graphNodes.map((n) => n.id));
      if (graphNodes.length) hasPriorGraph = true;
      graphNodes = nodes || [];
      ancestry = null;
      graphCompTokens = (tokens && tokens.comp) || null;
      graphAriaTokens = (tokens && tokens.aria) || null;
      render();

      if (host.drainClicks) {
        const ids = await host.drainClicks();
        if (ids && ids.length) flashRows(ids);
      }
      const selectors = [...new Set(graphNodes.map((n) => n.selector).filter(Boolean))];
      if (!selectors.length) return;

      // Ancestry is only fetched while the Tree view is actually showing: it
      // walks the DOM once per node, which is real work to spend on a view
      // nobody is looking at.
      if (host.ancestry) {
        $("view-tree").hidden = false;
        if (graphViewMode === "tree") {
          ancestry = await host.ancestry(selectors);
          render();
        }
      }
      if (host.validate) {
        const counts = await host.validate(selectors);
        if (counts) {
          selectorCounts = counts;
          render();
        }
      }
    }

    return { update, render, flashRows, element: wrap };
  }

  const api = { create, severityOf, getStability, estimateTokens, inferGroup };
  if (typeof window !== "undefined") window.ZeroDOMGraphUI = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
