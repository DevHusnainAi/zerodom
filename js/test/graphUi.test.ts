// Smoke test for the shared graph UI (zerodom/graph_ui.js), the file both the
// extension popup and the in-page sidebar render. It isn't part of the
// TypeScript port — it's plain browser JS with no build step — but this is the
// only place in the repo with a DOM to mount it in, so the check lives here.
//
// Covers the render path end to end: markup mounts, nodes group, severity and
// stability classes land on the right rows. Not the interaction handlers;
// linkedom has no layout or real event dispatch, and the value here is
// catching a broken render, which is the failure that would hit both hosts at
// once.

import { strict as assert } from "node:assert";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { parseHTML } from "linkedom";

const SOURCE = fileURLToPath(new URL("../../zerodom/graph_ui.js", import.meta.url));

const NODES = [
  { id: "01", type: "button", label: "Sign In", selector: "#signin" },
  { id: "02", type: "input", label: "Password", selector: ".pw", input_type: "password" },
  { id: "03", type: "link", label: "Delete account", selector: "a:nth-child(2)" },
];

function mount(nodes = NODES, tokens: any = null) {
  const { document, window } = parseHTML("<html><body><div id='root'></div></body></html>");
  (globalThis as any).window = window;
  (globalThis as any).document = document;
  new Function(readFileSync(SOURCE, "utf8"))();
  const ui = (window as any).ZeroDOMGraphUI.create(document.getElementById("root"), {
    setHTML: (el: any, html: string) => { el.innerHTML = html; },
    copy: () => {},
    toast: () => {},
    highlight: () => {},
    act: () => true,
    siteKey: () => "test",
    snapshotGet: async () => null,
    snapshotSet: async () => {},
    snapshotDel: async () => {},
  });
  return { document, ui, ready: ui.update(nodes, tokens) };
}

test("mounts and renders one row per node", async () => {
  const { document, ready } = mount();
  await ready;
  assert.equal(document.querySelectorAll(".graph-node").length, 3);
});

test("summary counts nodes and shows the token comparison", async () => {
  const { document, ready } = mount(NODES, { comp: { before: 900, after: 120 } });
  await ready;
  const text = document.querySelector(".summary").textContent;
  assert.match(text, /^3 nodes · \d+ tokens/);
  assert.match(text, /comp: 900 → 120/);
});

test("grouped view buckets by inferred group, not raw tag", async () => {
  const { document, ready } = mount();
  await ready;
  const groups = [...document.querySelectorAll(".graph-container-name")].map((e: any) => e.textContent);
  assert.deepEqual(groups, ["Form Controls", "Navigation"]);
});

test("security-relevant nodes get a severity class, plain ones don't", async () => {
  const { document, ready } = mount();
  await ready;
  // Sign In is unremarkable; the password field and the destructive link are
  // both flagged. Which *level* each lands on is pinned by the test below.
  assert.equal(document.querySelector('[data-node-id="01"]').className.includes("sev-"), false);
  assert.ok(document.querySelector('[data-node-id="02"]').className.includes("sev-"));
});

// Pins severityOf's real precedence, which is not what its comment claims: the
// sensitive/disabled/required checks are unconditional `if`s after the
// else-if chain, so they overwrite a higher level set earlier. A field that is
// BOTH type=password and labelled "Password" reports medium/"sensitive", not
// critical/"password". Ported verbatim from popup.js rather than corrected —
// changing it changes what a human is warned about, which is a call to make
// deliberately, not as a side effect of sharing the file.
test("trailing flags override the higher severity set earlier", () => {
  const { window } = parseHTML("<html><body></body></html>");
  (globalThis as any).window = window;
  new Function(readFileSync(SOURCE, "utf8"))();
  const severityOf = (window as any).ZeroDOMGraphUI.severityOf;

  assert.deepEqual(severityOf({ input_type: "password", label: "Secret" }), { level: "critical", tag: "password" });
  assert.deepEqual(severityOf({ input_type: "password", label: "Password" }), { level: "medium", tag: "sensitive" });
  assert.deepEqual(severityOf({ href: "javascript:x", label: "Go" }), { level: "critical", tag: "js: href" });
  assert.deepEqual(severityOf({ href: "javascript:x", label: "Go", disabled: true }), { level: "medium", tag: "disabled" });
  assert.equal(severityOf({ type: "button", label: "Sign In" }), null);
});

test("stability dot reflects the selector's shape", async () => {
  const { document, ready } = mount();
  await ready;
  const levels = [...document.querySelectorAll(".stability")].map(
    (e: any) => e.className.replace("stability ", "")
  );
  // #id -> stable, .class -> moderate, structural path -> fragile
  assert.deepEqual(levels, ["stability-stable", "stability-moderate", "stability-fragile"]);
});

test("a destructive-looking label raises a filter chip", async () => {
  const { document, ready } = mount();
  await ready;
  assert.ok(document.querySelector('[data-flag="destructive"]'), "expected a destructive chip");
});

test("an empty graph renders the empty state, not a broken list", async () => {
  const { document, ready } = mount([]);
  await ready;
  assert.equal(document.querySelectorAll(".graph-node").length, 0);
  assert.match(document.querySelector(".empty-state").textContent, /No graph data yet/);
});

test("the first paint marks nothing as new — there is no prior graph to diff", async () => {
  const { document, ready } = mount();
  await ready;
  assert.equal(document.querySelectorAll(".graph-node.diff-added").length, 0);
});

test("a node that appears in a later update is marked new", async () => {
  const { document, ui, ready } = mount();
  await ready;
  await ui.update([...NODES, { id: "04", type: "button", label: "Submit", selector: "#go" }], null);
  const added = document.querySelectorAll(".graph-node.diff-added");
  assert.equal(added.length, 1);
  assert.equal(added[0].dataset.nodeId, "04");
});

// ─── Tree view ────────────────────────────────────────────────────────────
// Ancestry is supplied by the host, so it can be handed in directly here —
// no DOM walk needed to test how the tree is shaped from it.
const TREE_NODES = [
  { id: "01", type: "link", label: "upvote", selector: "#a1" },
  { id: "02", type: "link", label: "Story One", selector: "#a2" },
  { id: "03", type: "link", label: "upvote", selector: "#b1" },
  { id: "04", type: "link", label: "Story Two", selector: "#b2" },
];
// Both stories sit under one <table>, each in its own <tr>, each link wrapped
// in a pointless single-child <td> — the shape real HTML actually has.
const ANCESTRY = {
  paths: {
    "#a1": ["tbl", "rowA", "tdA1"],
    "#a2": ["tbl", "rowA", "tdA2"],
    "#b1": ["tbl", "rowB", "tdB1"],
    "#b2": ["tbl", "rowB", "tdB2"],
  },
  labels: { tbl: "table#hnmain", rowA: "tr.athing", tdA1: "td.votelinks", tdA2: "td.title", rowB: "tr.athing", tdB1: "td.votelinks", tdB2: "td.title" },
};

function mountTree() {
  const { document, window } = parseHTML("<html><body><div id='root'></div></body></html>");
  (globalThis as any).window = window;
  (globalThis as any).document = document;
  new Function(readFileSync(SOURCE, "utf8"))();
  const ui = (window as any).ZeroDOMGraphUI.create(document.getElementById("root"), {
    setHTML: (el: any, html: string) => { el.innerHTML = html; },
    copy: () => {}, toast: () => {}, highlight: () => {}, act: () => true,
    ancestry: async () => ANCESTRY,
    siteKey: () => "test",
    snapshotGet: async () => null, snapshotSet: async () => {}, snapshotDel: async () => {},
  });
  return { document, ui };
}

test("the Tree button stays hidden unless the host can supply ancestry", async () => {
  const { document, ready } = mount();   // mount() has no ancestry in its host
  await ready;
  assert.equal(document.querySelector('[data-zd="view-tree"]').hidden, true);
});

test("Tree view nests nodes under the containers they share", async () => {
  const { document, ui } = mountTree();
  await ui.update(TREE_NODES, null);
  document.querySelector('[data-view="tree"]').dispatchEvent(new (globalThis as any).window.Event("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 0));

  const branches = [...document.querySelectorAll(".tree-label")].map((e: any) => e.textContent);
  // The two <td>s are single-child chains and collapse away; the <table> and
  // the two <tr>s are real branch points and survive.
  assert.deepEqual(branches, ["table#hnmain", "tr.athing", "tr.athing"]);
  assert.equal(document.querySelectorAll(".graph-node").length, 4);
});

test("Tree view indents deeper containers further", async () => {
  const { document, ui } = mountTree();
  await ui.update(TREE_NODES, null);
  document.querySelector('[data-view="tree"]').dispatchEvent(new (globalThis as any).window.Event("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 0));

  const depths = [...document.querySelectorAll(".tree-branch")].map(
    (e: any) => e.getAttribute("style")
  );
  assert.match(depths[0], /--d:\s*0/);   // the table
  assert.match(depths[1], /--d:\s*1/);   // a story row inside it
});
