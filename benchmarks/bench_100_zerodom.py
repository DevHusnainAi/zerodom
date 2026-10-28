import sys, json, time
from playwright.sync_api import sync_playwright
from zerodom.playwright_wrapper import ZeroDOM
import tiktoken
enc = tiktoken.get_encoding("cl100k_base"); tk = lambda s: len(enc.encode(s))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
sites = [l.strip() for l in open(sys.argv[1]) if l.strip() and not l.startswith("#")]
import os
done=set()
if os.path.exists(sys.argv[2]):
    done={json.loads(l)["i"] for l in open(sys.argv[2]) if l.strip()}
out = open(sys.argv[2], "a")
# No resource-abort route: blocking images/fonts degraded some renders (DDG read 35
# with it, 141 without). The network-idle gate (settle_network_ms) needs the real
# network to settle on, so let everything load.
with sync_playwright() as p:
    b = p.chromium.launch(headless=True); ctx = b.new_context(user_agent=UA)
    for i, url in enumerate(sites):
        if i in done:
            continue
        rec = {"i": i, "url": url, "nodes": 0, "tokens": 0, "ok": False}
        t0 = time.time()
        try:
            pg = ctx.new_page(); pg.goto(url, wait_until="load", timeout=40000)
            # from_page (not agent_snapshot) so we can record the honesty flags: a
            # near-empty read with hydration_pending/settle_error is a FAILURE, not
            # "this page has no controls" — it must not be silently excluded.
            g = ZeroDOM.from_page(pg, viewport_only=False)
            text = g.to_compact_text(terse=True); meta = g["metadata"]; pg.close()
            rec.update(nodes=len(g.ref_map()), tokens=tk(text), ok=True,
                       hydration_pending=bool(meta.get("hydration_pending")),
                       settle_error=bool(meta.get("settle_error")))
        except Exception as e:
            rec["err"] = type(e).__name__
        out.write(json.dumps(rec) + "\n"); out.flush()
        print(f"{i+1}/{len(sites)} {time.time()-t0:.0f}s nodes={rec['nodes']} tok={rec['tokens']} {url}", flush=True)
    b.close()
out.close()
