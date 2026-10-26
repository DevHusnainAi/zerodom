/**
 * Self-check for the TS port — not a full mirror of ../../tests/test_parser.py,
 * just one case per edge case that has a comment in parser.ts explaining why it
 * exists, so a regression here is caught before it reaches a real page.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { cssId, classToken, parseHtml, diffNodes } from "../dist/parser.js";

test("a duplicate id is not trusted as a unique anchor", () => {
  const graph = parseHtml(`
    <div id="dup"><a href="#">a</a></div>
    <div id="dup"><a href="#">b</a></div>
  `);
  assert.equal(graph.nodes.length, 2);
  assert.notEqual(graph.nodes[0].selector, graph.nodes[1].selector);
  assert.ok(!graph.nodes[0].selector.includes("#dup"));
});

test("numeric ids become attribute selectors, not an illegal #id", () => {
  assert.equal(cssId("49151933"), '[id="49151933"]');
  assert.equal(cssId("email"), "#email");
});

test("class with a dot uses an attribute selector, not two chained classes", () => {
  assert.equal(classToken("a.b"), "[class~='a.b']");
  assert.equal(classToken("btn"), ".btn");
});

test("table rows get the tbody a raw <table> implies", () => {
  const graph = parseHtml('<table><tr><td><a href="#">row</a></td></tr></table>');
  assert.equal(graph.selectorMap().node_01, "body > table > tbody > tr > td > a");
});

test("diffNodes delta on stable selectors (mirror of test_parser.py)", () => {
  const n = (i: number, label?: string) =>
    ({ id: "node_00", selector: `#a${i}`, type: "button", label: label ?? `B${i}` }) as any;
  const first = Array.from({ length: 10 }, (_, i) => n(i));
  const r1 = diffNodes(first);
  assert.equal(r1.refMap["01"], "#a0");
  assert.ok(r1.text.includes("[01] button B0") && !r1.text.includes("+"));

  const second = [n(0), n(1), n(2), n(3), n(4), n(5, "RENAMED"), n(6), n(7), n(8), n(10)];
  const r2 = diffNodes(second, r1.state);
  assert.ok(r2.text.includes("~[06] button RENAMED"));
  assert.ok(r2.text.includes("+[11] button B10"));
  assert.ok(r2.text.includes("-[10]"));
  assert.ok(r2.text.includes("(8 unchanged)"));
  assert.equal(diffNodes(second, r2.state).text, "(no change)");
});

test("diffNodes survives reflow on positional selectors (mirror of test_parser.py)", () => {
  const link = (pos: number, i: number) =>
    ({ id: "node_00", selector: `body > ul > li:nth-of-type(${pos}) > a`,
       type: "a", label: `Item ${i}`, href: `/p${i}` }) as any;
  const first = Array.from({ length: 5 }, (_, i) => link(i + 1, i));
  const r1 = diffNodes(first);
  const second = [{ id: "node_00", selector: "body > ul > li:nth-of-type(1) > a",
                    type: "a", label: "NEW", href: "/new" } as any,
                  ...Array.from({ length: 5 }, (_, i) => link(i + 2, i))];
  const r2 = diffNodes(second, r1.state);
  assert.ok(r2.text.includes("+[06] NEW"));
  assert.ok(r2.text.includes("(5 unchanged)"));
  assert.ok(!r2.text.includes("~"));                               // no churn
  assert.ok(r2.refMap["01"].endsWith("nth-of-type(2) > a"));       // Item 0 kept ref 01
});

test("diffNodes full-fallback when churn exceeds full (mirror of test_parser.py)", () => {
  const first = Array.from({ length: 4 }, (_, i) =>
    ({ id: "node_00", selector: `#a${i}`, type: "button", label: `B${i}` }) as any);
  const r1 = diffNodes(first);
  const second = Array.from({ length: 4 }, (_, i) =>
    ({ id: "node_00", selector: `#x${i}`, type: "button", label: `X${i}` }) as any);
  const r2 = diffNodes(second, r1.state);
  assert.ok(r2.text.startsWith("(refreshed"));
  assert.ok(r2.text.includes("button X0") && !r2.text.includes("+") && !r2.text.includes("~"));
});

test("terse format drops the link tag and label quotes (mirror of test_parser.py)", () => {
  const graph = parseHtml('<a href="/x">Go</a><button>Save</button>');
  const normal = graph.toCompactText();
  const terse = graph.toCompactText({ terse: true });
  assert.ok(terse.length < normal.length);
  assert.ok(terse.split("\n").some((l) => l.trim() === "[01] Go")); // link: no "a", no quotes
  assert.ok(terse.split("\n").some((l) => l.trim() === "[02] button Save")); // type kept, unquoted
  assert.ok(!terse.includes('"')); // no label quotes
});

test("refMap is keyed by the compact [NN] ref, not node_NN (mirror of test_parser.py)", () => {
  const graph = parseHtml('<a href="/a">one</a><button>two</button>');
  const refs = graph.refMap();
  // keyed by the exact token toCompactText prints — no node_ prefix
  assert.equal(refs["01"], graph.selectorMap().node_01);
  assert.equal(refs["02"], graph.selectorMap().node_02);
  assert.ok(!("node_01" in refs));
  for (const line of graph.toCompactText().split("\n").slice(1)) {
    const m = line.trimStart().match(/^\[(\w+)\]/);
    if (m) assert.ok(m[1] in refs, `compact ref [${m[1]}] missing from refMap`);
  }
});

test("a live-DOM tbody is not duplicated", () => {
  const graph = parseHtml('<table><tbody><tr><td><a href="#">row</a></td></tr></tbody></table>');
  assert.equal(graph.selectorMap().node_01, "body > table > tbody > tr > td > a");
});

test("bare <a> with no href is not a node; onclick makes any element one", () => {
  const graph = parseHtml('<a>not clickable</a><div onclick="x()">clickable</div>');
  assert.equal(graph.nodes.length, 1);
  assert.equal(graph.nodes[0].label, "clickable");
});

test("hidden subtrees are pruned, including their interactive children", () => {
  const graph = parseHtml('<div style="display:none"><button>hidden</button></div><button>visible</button>');
  assert.equal(graph.nodes.length, 1);
  assert.equal(graph.nodes[0].label, "visible");
});

test("hidden inputs are collected, not pruned, and stay out of the graph", () => {
  const html =
    "<form>" +
    '<input type="hidden" name="csrf_token" value="abc123">' +
    '<input type="hidden" id="draft_id" value="42">' +
    '<input name="q"><button>Submit</button>' +
    "</form>";
  const graph = parseHtml(html);
  assert.deepEqual(graph.nodes.map((n) => n.type), ["input", "button"]);
  assert.equal(graph.metadata.hidden_field_count, 2);
  assert.deepEqual(graph.metadata.hidden_fields, [
    { name: "csrf_token", value: "abc123", selector: "input[name='csrf_token']" },
    { name: "", value: "42", selector: "#draft_id", id: "draft_id" },
  ]);
  // Values must never leak into compact text (they cost context at graph time).
  assert.ok(!graph.toCompactText().includes("abc123"));
  assert.ok(!graph.toCompactText().includes("42"));
});

test("data-zerodom-offscreen node is dropped only when viewportOnly is requested", () => {
  const html = "<button>Onscreen</button><button data-zerodom-offscreen>Below fold</button>";
  assert.deepEqual(parseHtml(html).nodes.map((n) => n.label), ["Onscreen", "Below fold"]);
  const graph = parseHtml(html, "about:blank", true);
  assert.deepEqual(graph.nodes.map((n) => n.label), ["Onscreen"]);
  assert.equal(graph.metadata.offscreen_skipped, 1);
  assert.ok(graph.toCompactText().includes("1 more nodes offscreen"));
});

test("data-zerodom-occluded node is dropped only when checkOcclusion is requested", () => {
  const html = "<button>Reachable</button><button data-zerodom-occluded>Behind modal</button>";
  assert.deepEqual(parseHtml(html).nodes.map((n) => n.label), ["Reachable", "Behind modal"]);
  const graph = parseHtml(html, "about:blank", false, true);
  assert.deepEqual(graph.nodes.map((n) => n.label), ["Reachable"]);
  assert.equal(graph.metadata.occluded_skipped, 1);
  assert.ok(graph.toCompactText().includes("1 nodes hidden behind an overlay"));
});

test("repeated card items group ambiguous duplicate labels; a single-control card doesn't", () => {
  // Real Hacker News markup: the vote link carries no visible text (its arrow
  // is a CSS-styled <div>, not text) — only the title link does.
  const html =
    "<table>" +
    "<tr><td><a href='/vote?id=1' title='upvote'><div class='votearrow'></div></a></td>" +
    "<td><a href='/story?id=1'>Ask Academic mobile app</a></td></tr>" +
    "<tr><td><a href='/vote?id=2' title='upvote'><div class='votearrow'></div></a></td>" +
    "<td><a href='/story?id=2'>Recommended image viewer for Arch</a></td></tr>" +
    "</table>";
  const graph = parseHtml(html);
  assert.deepEqual(graph.nodes.map((n) => n.card), [
    "Ask Academic mobile app", "Ask Academic mobile app",
    "Recommended image viewer for Arch", "Recommended image viewer for Arch",
  ]);
  const text = graph.toCompactText();
  assert.ok(text.includes('@card "Ask Academic mobile app":\n  [01] a "upvote"'));

  const single = parseHtml("<table><tr><td><a href='/x'>Story number 0</a></td></tr></table>");
  assert.equal(single.nodes[0].card, undefined);
  assert.ok(!single.toCompactText().includes("@card"));
});

test("contenteditable div is a fillable node; contenteditable=false is not interactive", () => {
  const graph = parseHtml('<div contenteditable="true" id="composer">Type here</div>');
  assert.equal(graph.nodes[0].action, "fill");
  assert.equal(graph.nodes[0].role, "textbox");
  assert.equal(graph.nodes[0].content_editable, true);
  assert.equal(parseHtml('<div contenteditable="false">static</div>').nodes.length, 0);
});

test("label priority: for= beats wrapping label beats placeholder beats adjacent text", () => {
  const graph = parseHtml(`
    <label for="e">Email</label>
    <input id="e" placeholder="ph">
  `);
  assert.equal(graph.nodes[0].label, "Email");
});

test("adjacent caption text labels an uncaptioned input", () => {
  const graph = parseHtml("Search: <input name=\"q\">");
  assert.equal(graph.nodes[0].label, "Search");
});

test("submit input prefers its value over adjacent copy", () => {
  const graph = parseHtml('go: <input type="submit" value="Go">');
  assert.equal(graph.nodes[0].label, "Go");
});

test("image-only link uses img alt", () => {
  const graph = parseHtml('<a href="#"><img src="x.png" alt="Logo"></a>');
  assert.equal(graph.nodes[0].label, "Logo");
});

test("compact text marks required with * and hides selectors by default", () => {
  const graph = parseHtml('<input id="e" required placeholder="Email">', "https://x.test/login");
  const text = graph.toCompactText();
  assert.ok(text.includes("[01] input*"));
  assert.ok(!text.includes("#e"));
});

test("an empty-fragment anchor (<a href=\"#x\" id=\"x\"></a>) is a destination, not a click target", () => {
  const graph = parseHtml('<a href="#section" id="section"></a><a href="#top">Back to top</a>');
  assert.equal(graph.nodes.length, 1);
  assert.equal(graph.nodes[0].label, "Back to top");
});

test("shadow DOM: a light-tree element with no shadow twin is unscoped", () => {
  const graph = parseHtml(`
    <div id="host"><template shadowrootmode="open"><button>in shadow</button></template></div>
    <button id="light">in light DOM</button>
  `);
  const light = graph.nodes.find((n) => n.label === "in light DOM")!;
  assert.equal(light.selector, "#light");
});

test("shadow DOM: a light element colliding with its shadow twin is scoped with :light()", () => {
  const graph = parseHtml(`
    <div id="host">
      <template shadowrootmode="open"><button>shadow twin</button></template>
      <button>shadow twin</button>
    </div>
  `);
  const selectors = Object.values(graph.selectorMap());
  assert.ok(selectors.some((s) => s.startsWith(":light(")), selectors.join(", "));
  assert.ok(selectors.some((s) => s.endsWith(">> nth=1")), selectors.join(", "));
});

test("shadow DOM: closed-root content is never claimed as a node, the host is recorded", () => {
  const graph = parseHtml(`
    <div id="host"><button>Light</button><template shadowrootmode="closed">
      <button>Secret</button></template></div>
  `);
  assert.ok(!graph.nodes.some((n) => n.label === "Secret"), "closed content must not become nodes");
  assert.ok(graph.nodes.some((n) => n.label === "Light"));
  assert.deepEqual(graph.metadata.closed_shadow_hosts, ["#host"]);
  assert.equal(graph.metadata.closed_shadow_host_count, 1);
});

test("shadow DOM: an empty custom element host is recorded as unmapped", () => {
  const graph = parseHtml("<x-panel></x-panel><p>hi</p>");
  assert.equal(graph.metadata.closed_shadow_host_count, 1);
  assert.ok(graph.metadata.closed_shadow_hosts!.some((s) => s.includes("x-panel")));
  // with light children it is not an unmapped host
  const withKids = parseHtml("<x-panel><button>Go</button></x-panel>");
  assert.equal(withKids.metadata.closed_shadow_hosts, undefined);
});

test("tooltip library attributes label an icon-only button", () => {
  const graph = parseHtml('<button data-tippy-content="Close dialog"><svg></svg></button>');
  assert.equal(graph.nodes[0].label, "Close dialog");
});

test("an unlabelled link falls back to its href", () => {
  const graph = parseHtml('<a href="https://example.com/pricing"></a>');
  assert.equal(graph.nodes[0].label, "example.com/pricing");
});

test("an unlabelled div with onclick falls back to the handler's name", () => {
  const graph = parseHtml('<div onclick="_bsa.closeModal()"><svg></svg></div>');
  assert.equal(graph.nodes[0].label, "close modal");
});

test("href fallback skips javascript:/data:/vbscript: targets, case-insensitively", () => {
  for (const href of [
    "#",
    "javascript:void(0)",
    "JavaScript:void(0)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
  ]) {
    const graph = parseHtml(`<a href="${href}"><img src="i.png"></a>`);
    assert.equal(graph.nodes[0].label, "Unlabelled Element", href);
  }
});

test("caption stripping stays fast on an adversarial run of strip characters", () => {
  // Regression for a ReDoS in the old /[ \t\n :*>\-–—]+$/ regex: an unanchored
  // search retries from every position in a long run of strip chars that isn't
  // right at the string's end, O(n^2). 50k tabs followed by non-matching text
  // used to hang; the linear rewrite finishes in milliseconds either way.
  const adversarial = "\t".repeat(50_000) + "x";
  const start = performance.now();
  const graph = parseHtml(`${adversarial}<input>`);
  assert.ok(performance.now() - start < 1000, "caption stripping should stay linear");
  assert.equal(graph.nodes.length, 1);
});

test("a near-empty small page warns it's probably a bot wall", () => {
  const graph = parseHtml("<p>blocked</p>");
  assert.ok(graph.metadata.warning?.includes("bot wall"));
});

test("latency stays fast on a few thousand nodes (best-of-N)", () => {
  // Best-of-N: a perf gate on wall-clock flakes when another process steals the
  // CPU mid-parse; the fastest of a few runs is the real cost, immune to that.
  // Ceiling is coarse on purpose — catches a gross/algorithmic regression, not
  // runner jitter. Mirror of tests/test_parser.py best-of-N latency gates.
  const rows = Array.from({ length: 3000 }, (_, i) => `<tr><td><a href="#${i}">row ${i}</a></td></tr>`).join("");
  const html = `<table>${rows}</table>`;
  let best = Infinity;
  let graph = parseHtml(html);
  for (let i = 0; i < 5; i++) {
    graph = parseHtml(html);
    best = Math.min(best, graph.metadata.parsing_latency_ms as number);
  }
  assert.equal(graph.nodes.length, 3000);
  assert.ok(best < 200, `took ${best}ms`);
});

// Mirrors test_collapsing_duplicates_keeps_document_order in tests/test_parser.py.
// collapseDuplicates used to rebuild the list by walking the (type, role, label)
// groups, emitting every same-labelled node together — which fragmented the
// @card runs in toCompactText() and reordered the graph away from the page.
test("collapsing duplicates keeps document order", () => {
  const html = "<body>" + [1, 2, 3].map((i) =>
    `<li><a href="/p${i}">Post ${i}</a><a href="/like/${i}">Like</a><a href="/share/${i}">Share</a></li>`
  ).join("") + "</body>";

  const graph = parseHtml(html, "https://e.com", false, false, true);
  assert.deepEqual(graph.nodes.map((n: any) => n.label), [
    "Post 1", "Like", "Share",
    "Post 2", "Like", "Share",
    "Post 3", "Like", "Share",
  ]);

  const cards = graph.toCompactText().split("\n").filter((l: string) => l.startsWith("@card"));
  assert.equal(cards.length, 3);
  assert.equal(new Set(cards).size, 3);
});

test("collapsing duplicates still collapses undifferentiated repeats", () => {
  const html = "<body>" + "<div><button>More actions</button></div>".repeat(5) + "</body>";
  const graph = parseHtml(html, "https://e.com", false, false, true);
  assert.equal(graph.nodes.length, 1);
  assert.equal((graph.nodes[0] as any).count, 5);
  assert.equal(graph.metadata.duplicates_collapsed, 4);
});

test("challenge pages flag the vendor in metadata (mirror of test_parser.py)", () => {
  const cases: Record<string, string> = {
    '<script src="/cdn-cgi/challenge-platform/h/b/x.js"></script>': "cloudflare",
    '<div class="cf-turnstile" data-sitekey="x"></div>': "turnstile",
    '<div class="g-recaptcha"></div>': "recaptcha",
    '<div class="h-captcha"></div>': "hcaptcha",
  };
  for (const [frag, kind] of Object.entries(cases)) {
    const g = parseHtml(`<html><body>${frag}</body></html>`, "https://t.example");
    assert.equal(g.metadata.blocked?.kind, kind, frag);
  }
  const clean = parseHtml("<html><body><a href=/x>hi</a></body></html>", "https://x.example");
  assert.equal(clean.metadata.blocked, undefined);
});
