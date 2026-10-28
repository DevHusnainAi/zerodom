/**
 * Self-check for the network-idle gate + navigation retry in fromPage (mirror of
 * ../../tests/test_settle_gate.py). Pure stub, no browser.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { ZeroDOM } from "../dist/playwright.js";

const HTML = "<html><body><a href='/a'>A</a></body></html>";

function tag(fn: string): string {
  if (fn.includes("new Promise")) return "settle";
  if (fn.trim().startsWith("() =>")) return "hydration";
  return "serialize";
}

function stub(settleRaises = 0) {
  const calls: string[] = [];
  let settleAttempts = 0;
  const page: any = {
    url: () => "http://x",
    calls,
    get settleAttempts() { return settleAttempts; },
    async waitForLoadState(state: string, opts?: { timeout?: number }) {
      calls.push(`wait:${state}:${opts?.timeout}`);
    },
    async evaluate(fn: string) {
      const t = tag(fn);
      if (t === "settle") {
        settleAttempts++;
        if (settleAttempts <= settleRaises) throw new Error("Execution context was destroyed");
        return undefined;
      }
      calls.push(`eval:${t}`);
      if (t === "hydration") return { hydrationPending: false };
      return HTML;
    },
    async content() { return HTML; },
    frames: () => [],
  };
  return page;
}

test("network-idle gate runs before serialize", async () => {
  const p = stub();
  await ZeroDOM.fromPage(p);
  assert.equal(p.calls[0], "wait:networkidle:3000");
  assert.ok(p.calls.indexOf("wait:networkidle:3000") < p.calls.indexOf("eval:serialize"));
});

test("settleNetworkMs:0 disables the gate", async () => {
  const p = stub();
  await ZeroDOM.fromPage(p, { settleNetworkMs: 0 });
  assert.ok(!p.calls.some((c: string) => c.startsWith("wait:")));
});

test("navigation mid-settle retries once then succeeds", async () => {
  const p = stub(1);
  const graph = await ZeroDOM.fromPage(p);
  assert.equal(p.settleAttempts, 2);
  assert.ok(!(graph.metadata as any).settle_error);
});

test("persistent settle failure is flagged, not a silent empty read", async () => {
  const p = stub(99);
  const graph = await ZeroDOM.fromPage(p);
  assert.equal(p.settleAttempts, 2);
  assert.equal((graph.metadata as any).settle_error, true);
});
