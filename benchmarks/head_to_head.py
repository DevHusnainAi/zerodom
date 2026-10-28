"""ZeroDOM vs agent-browser, head to head, on the SAME page with KNOWN state.

The token benchmark (bench_agent_snapshot.py) already shows ZeroDOM is cheaper. This asks
the other question honestly: is ZeroDOM's state capture actually MORE accurate than
agent-browser's, or just cheaper? To answer without guessing, it serves one controlled
page whose live DOM is mutated through *properties* (so they diverge from the HTML
attributes), snapshots it with BOTH tools, and checks each tool's output against the state
we know we set — plus token cost and two security/legibility details.

Needs:  npm i -g agent-browser && agent-browser install
Run:    uv run python benchmarks/head_to_head.py
"""

from __future__ import annotations

import http.server
import socketserver
import subprocess
import sys
import threading

from playwright.sync_api import sync_playwright

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
    def toks(s: str) -> int: return len(_ENC.encode(s))
except Exception:
    def toks(s: str) -> int: return len(s) // 4

PORT = 8781
PAGE = """<!doctype html><html><head><title>H2H</title></head><body><form>
  <input id=c1 type=checkbox><label for=c1>Notify</label>
  <input id=t1 type=text aria-label="Company">
  <input id=pw type=password aria-label="Password">
  <input id=mt type=text aria-label="Empty">
  <select id=s aria-label="Country"><option>USA</option><option>Canada</option></select>
  <button aria-expanded="false">Advanced</button>
  <div role=tab aria-selected="true" tabindex=0>Overview</div>
  <div role=tab tabindex=0>Reports</div>
</form><script>
  document.getElementById('c1').checked = true;      /* checked via property, NO attribute */
  document.getElementById('t1').value = 'Acme Inc';  /* value via property */
  document.getElementById('pw').value = 'hunter2';   /* sensitive value present */
  document.getElementById('s').value = 'Canada';     /* 2nd option selected at runtime */
</script></body></html>"""

# (name, truth-in-DOM, substring that proves agent-browser got it, same for ZeroDOM)
CHECKS = [
    ("checkbox checked (via property, no attr)", "Notify checked", "Notify\" [checked=true", "Notify x"),
    ("text value",                               "Acme Inc",       ": Acme Inc",             "=Acme Inc"),
    ("select current value",                     "Canada",         "Country\" [expanded=false, ref=e5]: Canada", "Country =Canada"),
    ("disclosure collapsed",                     "expanded=false", "Advanced\" [expanded=false", "Advanced closed"),
    ("active tab",                               "selected",       "Overview\" [selected",   "Overview *"),
]


class _Q(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(PAGE.encode())
    def log_message(self, *a): pass


def _ab(url: str) -> str:
    subprocess.run(["agent-browser", "open", url], capture_output=True, text=True, timeout=60)
    out = subprocess.run(["agent-browser", "snapshot", "-i"], capture_output=True, text=True, timeout=60)
    return out.stdout


def _zd(url: str) -> str:
    from zerodom.playwright_wrapper import ZeroDOM
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True); pg = b.new_page()
        pg.goto(url, wait_until="load", timeout=30000)
        text, _ = ZeroDOM.agent_snapshot(pg)
        b.close()
    return text


def main() -> int:
    srv = socketserver.TCPServer(("127.0.0.1", PORT), _Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/page.html"
    try:
        ab, zd = _ab(url), _zd(url)
    finally:
        srv.shutdown()

    print("=== agent-browser ===\n" + ab.strip() + "\n\n=== ZeroDOM ===\n" + zd.strip() + "\n")
    print(f"{'state':<42} {'agent-browser':<14} {'ZeroDOM':<10}")
    ab_ok = zd_ok = 0
    for name, _truth, ab_sub, zd_sub in CHECKS:
        a, z = ab_sub in ab, zd_sub in zd
        ab_ok += a; zd_ok += z
        print(f"  {name:<40} {'OK' if a else 'MISS':<14} {'OK' if z else 'MISS':<10}")
    n = len(CHECKS)
    print(f"\n  STATE ACCURACY   agent-browser {ab_ok}/{n}    ZeroDOM {zd_ok}/{n}")
    print(f"  TOKENS           agent-browser {toks(ab)}       ZeroDOM {toks(zd)}  "
          f"({100*(toks(ab)-toks(zd))/toks(ab):.0f}% fewer)")
    # security / legibility details
    pw_len_leak = "•••" in ab
    print(f"  PASSWORD         agent-browser masks but renders {ab.count(chr(8226))} bullets "
          f"(= value length){' — leaks length' if pw_len_leak else ''};  ZeroDOM: "
          f"{'filled, no length' if 'Password filled' in zd else '?'}")
    print(f"  EMPTY FIELD      agent-browser: {'implicit (absence)' if 'Empty\" [ref' in ab or 'Empty [ref' in ab else '?'};  "
          f"ZeroDOM: {'explicit `empty`' if 'Empty empty' in zd else '?'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
