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
with sync_playwright() as p:
    b = p.chromium.launch(headless=True); ctx = b.new_context(user_agent=UA)
    ctx.route("**/*", lambda r: (r.abort if r.request.resource_type in ("image","media","font") else r.continue_)())
    for i, url in enumerate(sites):
        if i in done:
            continue
        rec = {"i": i, "url": url, "nodes": 0, "tokens": 0, "ok": False}
        t0 = time.time()
        try:
            pg = ctx.new_page(); pg.goto(url, wait_until="load", timeout=40000)
            text, refmap = ZeroDOM.agent_snapshot(pg); pg.close()
            rec.update(nodes=len(refmap), tokens=tk(text), ok=True)
        except Exception as e:
            rec["err"] = type(e).__name__
        out.write(json.dumps(rec) + "\n"); out.flush()
        print(f"{i+1}/{len(sites)} {time.time()-t0:.0f}s nodes={rec['nodes']} tok={rec['tokens']} {url}", flush=True)
    b.close()
out.close()
