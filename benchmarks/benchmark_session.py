"""Session-reuse benchmark: same URL fetched three ways, as a markdown table.

    uv run benchmarks/benchmark_session.py                    # local fixture (always)
    ZERODOM_E2E=1 uv run benchmarks/benchmark_session.py      # + live target
    ZERODOM_CHROME=<path> uv run benchmarks/benchmark_session.py  # pick the stealth binary

Three transports per target:
  headless  cli.fetch(render=True)  -- fresh Chromium, no cookies
  stealth   cdp_pipe.fetch          -- throwaway-profile Chrome over a CDP pipe
  relay     the operator's real Chrome through the WS relay (session included)

Records, per fetch: nodes parsed, whether a bot-wall fingerprint survived in
metadata["blocked"], where the fetch actually landed, and seconds. The local
fixture has /public, a cookie-gated /authed (302 -> /login without the session
cookie) and /wall carrying inline Turnstile markers -- so every run shows the
authenticated-route contrast (app vs login redirect) without touching the
network. The relay leg seeds the fixture's session cookie (an already-logged-in
Chrome, the state relay reuse exists to carry) and removes it afterwards; the
live row under ZERODOM_E2E=1 uses the operator's real session as-is. Relay
rows skip honestly when no relay is listening -- this script never spawns one,
and never closes the operator's browser (own tab only, crawl --relay precedent).
ZERODOM_NO_SOLVER=1 is set for the whole run: this measures session reuse, not
solver spend. Output: benchmarks/SESSION.md.
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from zerodom.parser import ZeroDOMParser

HERE = pathlib.Path(__file__).parent

PUBLIC = (
    '<html><head><title>Public</title></head><body>'
    "<nav><a href=\"/\">Home</a><a href=\"/public\">Public</a>"
    '<a href="/wall">Status</a></nav>'
    "<h1>Fixture public page</h1>"
    '<form action="/login" method="get"><label for="q">Search</label>'
    '<input id="q" name="q"><button>Search</button></form>'
    '<footer><a href="/login">Sign in</a></footer></body></html>'
)
LOGIN = (
    '<html><head><title>Sign in</title></head><body><h1>Sign in</h1>'
    '<form action="/authed" method="post">'
    '<label for="u">Username</label><input id="u" name="u">'
    '<label for="p">Password</label><input id="p" name="p" type="password">'
    "<button>Sign in</button></form>"
    '<a href="/public">Back</a></body></html>'
)
DASHBOARD = (
    '<html><head><title>Orders</title></head><body>'
    '<nav><a href="/authed">Orders</a><a href="/public">Public</a>'
    '<a href="/logout">Sign out</a></nav>'
    "<h1>Fixture dashboard (authed)</h1>"
    "<table>" + "".join(
        f"<tr><td>ORD-000{i}</td>"
        f"<td><button>Approve ORD-000{i}</button></td></tr>"
        for i in range(1, 7)
    ) + "</table>"
    '<form><label for="po">PO</label><input id="po" name="po">'
    "<button>Export</button></form></body></html>"
)
WALL = (
    "<html><head><title>Just a moment...</title></head><body>"
    "<h1>Just a moment...</h1>"
    '<div class="cf-turnstile" data-sitekey="0xAAAA"></div>'
    '<form><input name="a"><button>Verify</button></form></body></html>'
)
SESSION_COOKIE = {"name": "zd_session", "value": "fixture-ok"}


class _Fixture(BaseHTTPRequestHandler):
    """The authenticated-route ground truth: /authed is 302 -> /login unless
    the session cookie rides along, /wall is a parse-level Turnstile wall."""

    def do_GET(self):
        path = self.path.split("?")[0]
        cookie = self.headers.get("Cookie", "")
        if path == "/authed":
            if f"{SESSION_COOKIE['name']}={SESSION_COOKIE['value']}" in cookie:
                body = DASHBOARD.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
            return
        pages = {"/": PUBLIC, "/public": PUBLIC, "/login": LOGIN, "/wall": WALL}
        body = pages.get(path, "<html><body>not found</body></html>").encode()
        self.send_response(404 if path not in pages else 200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # keep the table's stdout clean
        pass


def _landed(final: str) -> str:
    """Where the fetch ended, origin stripped: /login, /authed, /#/, ..."""
    parts = urlsplit(final)
    out = parts.path or "/"
    if parts.query:
        out += "?" + parts.query
    if parts.fragment:
        out += "#" + parts.fragment
    return out


def _measure(html: str, final: str, elapsed: float) -> dict:
    graph = ZeroDOMParser(html, final).parse()
    meta = graph["metadata"]
    blocked = meta.get("blocked")
    return {
        "nodes": meta.get("total_interactive_nodes", 0),
        "wall": blocked["kind"] if isinstance(blocked, dict) else "—",
        "landed": _landed(final),
        "seconds": round(elapsed, 2),
        "note": "",
    }


def _skip(reason: str) -> dict:
    return {"nodes": "—", "wall": "—", "landed": "—", "seconds": "—",
            "note": reason}


def _error(exc: Exception) -> dict:
    first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return {"nodes": "—", "wall": "—", "landed": "—", "seconds": "—",
            "note": f"error: {first[:90]}"}


def _fetch_headless(url: str) -> dict:
    from zerodom.cli import fetch as cli_fetch

    t0 = time.monotonic()
    try:
        html, final = cli_fetch(url, True)
    except Exception as exc:
        return _error(exc)
    return _measure(html, final, time.monotonic() - t0)


def _fetch_stealth(url: str) -> dict:
    from zerodom import cdp_pipe

    try:
        cdp_pipe.find_chrome()
    except Exception as exc:
        return _skip(f"no chrome binary (set ZERODOM_CHROME): {str(exc)[:70]}")
    t0 = time.monotonic()
    try:
        html, final = cdp_pipe.fetch(url)
    except Exception as exc:
        return _error(exc)
    return _measure(html, final, time.monotonic() - t0)


def _fetch_relay(url: str, seed_url: str | None) -> dict:
    """Attach through an already-listening relay (never spawn one), open our
    own tab, navigate exactly as `crawl --relay` does, then close only our tab.
    `seed_url`, when set, borrows the fixture session for this leg and gives
    it back afterwards -- a stand-in for the cookies the operator's Chrome
    already holds against a real app."""
    from playwright.sync_api import sync_playwright

    from zerodom.cli import _connect_relay, _goto, _relay_reachable
    from zerodom.playwright_wrapper import serialize
    from zerodom.relay import DEFAULT_PORT

    # Probe before starting the driver: entering sync_playwright only to raise
    # SystemExit races the driver's own init and spews torn-down-task noise.
    endpoint = os.environ.get("ZERODOM_CDP_ENDPOINT") or (
        f"ws://127.0.0.1:{DEFAULT_PORT}/cdp/local" if _relay_reachable(DEFAULT_PORT) else None)
    if not endpoint:
        return _skip("no relay listening — run `zerodom relay` + the extension "
                     "(or set ZERODOM_CDP_ENDPOINT)")
    with sync_playwright() as pw:
        try:
            browser = _connect_relay(pw)
        except SystemExit as exc:
            return _skip(str(exc).removeprefix("zerodom: "))
        except Exception as exc:
            return _skip(f"relay unreachable: {str(exc)[:70]}")
        ctx = browser.contexts[0]
        own_tab = None
        try:
            own_tab = ctx.new_page()
            if seed_url:
                ctx.add_cookies([{**SESSION_COOKIE, "url": seed_url}])
            t0 = time.monotonic()
            _goto(own_tab, url, "relay")
            html, final = serialize(own_tab), own_tab.url
            return _measure(html, final, time.monotonic() - t0)
        except Exception as exc:
            return _error(exc)
        finally:
            if seed_url:
                try:
                    ctx.clear_cookies(name=SESSION_COOKIE["name"], url=seed_url)
                except Exception:
                    pass  # the fixture dies with this process anyway
            if own_tab is not None:
                try:
                    own_tab.close()
                except Exception:
                    pass
            # Never browser.close() on a relay browser: it's the operator's
            # real Chrome -- leaving the with-block only stops our connection.


# The classic public instance (juice-shop.herokuapp.com) goes down for long
# stretches; ZERODOM_LIVE_TARGET points the live rows at any juice-shop
# deployment (e.g. a local `docker run bkimminich/juice-shop`) untouched.
_LIVE_URL = os.environ.get("ZERODOM_LIVE_TARGET", "https://juice-shop.herokuapp.com/")
LIVE = (urlsplit(_LIVE_URL).netloc or _LIVE_URL, _LIVE_URL)


def main(argv: list[str]) -> int:
    # This benchmark measures session reuse; solver spend belongs to §3.6's
    # clearance benchmark, so cap the ladder here for every transport.
    os.environ.setdefault("ZERODOM_NO_SOLVER", "1")

    srv = HTTPServer(("127.0.0.1", 0), _Fixture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    root = f"http://127.0.0.1:{srv.server_port}"

    targets = [
        ("fixture/public", f"{root}/public"),
        ("fixture/authed", f"{root}/authed"),
        ("fixture/wall", f"{root}/wall"),
    ]
    if os.environ.get("ZERODOM_E2E"):
        targets.append(LIVE)

    rows: list[str] = [
        "| target | transport | nodes | wall | landed | seconds | note |",
        "|---|---|---|---|---|---:|---|",
    ]

    def add(target: str, transport: str, res: dict) -> None:
        line = (f"| {target} | {transport} | {res['nodes']} | {res['wall']} | "
                f"{res['landed']} | {res['seconds']} | {res['note']} |")
        rows.append(line)
        print(line, flush=True)

    for name, url in targets:
        seed = root if name.startswith("fixture/") else None
        add(name, "headless", _fetch_headless(url))
        add(name, "stealth", _fetch_stealth(url))
        add(name, "relay", _fetch_relay(url, seed))

    srv.shutdown()

    notes = [
        "",
        "## Reading this",
        "",
        "- **fixture/authed** is the authenticated-route ground truth: without the",
        "  session cookie the fresh-profile legs follow the 302 and land on",
        "  `/login`; the relay leg lands on `/authed` because the cookie rides",
        "  with the browser (seeded by this script for the fixture, removed",
        "  afterwards; live rows use the operator's real session as-is).",
        "- **fixture/wall** carries inline Turnstile markers: a carried session",
        "  does not clear a wall -- every leg keeps `metadata[\"blocked\"]`, and",
        "  clearing is the clearance ladder's job (benchmark_clearance.py / §3.6).",
        "- **skipped**: no relay listening or no extension attached (this script",
        "  never spawns one, never closes the operator's browser), or no Chrome",
        "  binary for the stealth leg (`ZERODOM_CHROME=<path>` picks one).",
        "- seconds are wall-clock per fetch, including browser launch on the",
        "  fresh-profile legs -- that cost is what relay reuse skips.",
        "",
        "## Run",
        "",
        f"- date: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"- e2e: {'1 (live rows included)' if os.environ.get('ZERODOM_E2E') else '0 (fixture only)'}",
        f"- chrome: {os.environ.get('ZERODOM_CHROME') or 'PATH'}",
        f"- no_solver: {os.environ.get('ZERODOM_NO_SOLVER')}",
        "",
    ]
    out = [
        "# Session reuse: headless vs stealth vs relay",
        "",
        "`uv run benchmarks/benchmark_session.py` writes this table;",
        "`ZERODOM_E2E=1` adds the live target. One row per (target, transport).",
        "",
        *rows,
        *notes,
    ]
    (HERE / "SESSION.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\nwrote {HERE / 'SESSION.md'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
