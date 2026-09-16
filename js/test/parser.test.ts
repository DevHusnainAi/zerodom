/**
 * Self-check for the TS port — not a full mirror of ../../tests/test_parser.py,
 * just one case per edge case that has a comment in parser.ts explaining why it
 * exists, so a regression here is caught before it reaches a real page.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { cssId, classToken, parseHtml } from "../dist/parser.js";

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

test("a near-empty small page warns it's probably a bot wall", () => {
  const graph = parseHtml("<p>blocked</p>");
  assert.ok(graph.metadata.warning?.includes("bot wall"));
});

test("latency is well under the 50ms budget on a few thousand nodes", () => {
  const rows = Array.from({ length: 3000 }, (_, i) => `<tr><td><a href="#${i}">row ${i}</a></td></tr>`).join("");
  const graph = parseHtml(`<table>${rows}</table>`);
  assert.equal(graph.nodes.length, 3000);
  assert.ok(graph.metadata.parsing_latency_ms < 200, `took ${graph.metadata.parsing_latency_ms}ms`);
});
