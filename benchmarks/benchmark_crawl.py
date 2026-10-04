"""Crawl-coverage benchmark: routes + API endpoints discovered, headless vs relay.

    uv run benchmarks/benchmark_crawl.py                    # local fixture (always)
    ZERODOM_E2E=1 uv run benchmarks/benchmark_crawl.py      # + OWASP Juice Shop
    ZERODOM_CRAWL_MAX_PAGES=40 uv run benchmarks/benchmark_crawl.py

Two transports, both through zerodom.cli.crawl_site (same BFS, scope, deny):
  headless  fresh Chromium (_launch) — no cookies, no session
  relay     the operator's real Chrome through the WS relay (--relay path);
            for the fixture the session cookie is seeded first and removed
            afterwards (benchmark_session precedent). Skips honestly when no
            relay is listening — this script never spawns one and never closes
            the operator's browser.

Recall = (routes + API endpoints discovered) / ground truth, per leg.
The fixture ground truth is local and fully deterministic: the guest nav links
only public routes and the authed ones sit behind the zd_session cookie, so
headless stalls at 4/7 routes + 2/4 APIs while relay (seeded) reaches every
authed route and endpoint — its single route miss is /login, because the authed
nav hides the sign-in link (real app behavior, not a crawler bug).
The Juice Shop ground truth is curated from the source repo (app.routing.ts
route paths + service GET endpoints, verified against master on 2026-10-05) and
its rows run only under ZERODOM_E2E=1; an unreachable target is reported as
such, never papered over. juice-shop's nav lives behind a hamburger drawer that
is closed at every tested viewport — read-only BFS never clicks, so its live
recall honestly stops at the entry point (the fixture carries the deterministic
session contrast; CRAWL.md's notes say why). Read-only: crawl never submits a form, so POST-only
endpoints are not in the truth set and an endpoint counts as discovered only
when a page load actually fires it — conservative by construction.

WSTG framing (OWASP WSTG latest, 4.1 Information Gathering): the counts below
evidence 4.1.4 Attack Surface Identification (routes+endpoints inventory),
4.1.6 Identify Application Entry Points (forms with action/method/inputs),
4.1.7 Map Execution Paths Through Application (XHR/fetch each load fires) and
4.1.10 Map Application Architecture (link structure). The crawl does NOT claim
4.1.1–4.1.3 / 4.1.5 / 4.1.8–4.1.9 (search-engine recon, server/framework
fingerprinting, metafiles, content review) — different techniques entirely.

ZERODOM_NO_SOLVER=1 caps the clearance ladder for the whole run: this measures
coverage, not wall time (§3.6 owns clearance). Output: benchmarks/CRAWL.md.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

HERE = pathlib.Path(__file__).parent

SESSION_COOKIE = {"name": "zd_session", "value": "fixture-ok"}
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---------------------------------------------------------------- fixture app

PUBLIC_ROUTES = ["/", "/products", "/contact", "/login"]
AUTHED_ROUTES = ["/account", "/orders", "/settings"]
PUBLIC_APIS = ["/api/products", "/api/feedback"]
AUTHED_APIS = ["/api/account", "/api/orders"]
FIXTURE_ROUTES = PUBLIC_ROUTES + AUTHED_ROUTES
FIXTURE_APIS = PUBLIC_APIS + AUTHED_APIS


def _nav(authed: bool) -> str:
    """Server-rendered nav: the guest nav links /login; the authed nav swaps it
    for Account/Orders/Settings — the whole headless-vs-relay contrast hangs
    on this one line."""
    links = ['<a href="/">Home</a>', '<a href="/products">Products</a>',
             '<a href="/contact">Contact</a>']
    if authed:
        links += ['<a href="/account">Account</a>', '<a href="/orders">Orders</a>',
                  '<a href="/settings">Settings</a>']
    else:
        links += ['<a href="/login">Login</a>']
    return "<nav>" + "".join(links) + "</nav>"


def _page(title: str, authed: bool, body: str, apis: list[str]) -> str:
    script = "".join(f"<script>fetch('{a}')</script>" for a in apis)
    return (
        f"<html><head><title>{title}</title></head><body>"
        f"{_nav(authed)}<h1>{title}</h1>{body}{script}"
        "</body></html>"
    )


PAGES = {
    "/": _page("Home", False,
               "<p>Fixture app home.</p>", PUBLIC_APIS),
    "/products": _page("Products", False,
                       "<ul><li>Widget</li><li>Gadget</li></ul>", ["/api/products"]),
    "/contact": _page("Contact", False,
                      '<form action="/contact" method="post">'
                      '<label for="n">Name</label><input id="n" name="name">'
                      '<label for="m">Message</label><input id="m" name="message">'
                      "<button>Send</button></form>", []),
    "/login": _page("Login", False,
                    '<form action="/login" method="post">'
                    '<label for="u">Username</label><input id="u" name="username">'
                    '<label for="p">Password</label><input id="p" name="password" type="password">'
                    "<button>Sign in</button></form>", []),
    "/account": _page("Account", True,
                      '<form action="/account" method="post">'
                      '<label for="e">Email</label><input id="e" name="email">'
                      "<button>Save</button></form>", ["/api/account"]),
    "/orders": _page("Orders", True,
                     "<table><tr><td>ORD-1</td></tr></table>", ["/api/orders"]),
    "/settings": _page("Settings", True,
                       '<form action="/settings" method="post">'
                       '<label for="t">Theme</label><select id="t" name="theme">'
                       "<option>light</option></select><button>Apply</button></form>", []),
}
AUTHED_PAGES = {r: PAGES[r] for r in AUTHED_ROUTES}


class _Fixture(BaseHTTPRequestHandler):
    """Guest sees the public nav; authed routes 302 to /login without the
    session cookie; authed APIs 401 without it. The crawl never POSTs, so the
    POST handlers exist only so a stray submit can't 500."""

    def do_GET(self):
        path = self.path.split("?")[0]
        cookie = self.headers.get("Cookie", "")
        authed = f"{SESSION_COOKIE['name']}={SESSION_COOKIE['value']}" in cookie
        if path in AUTHED_PAGES:
            if not authed:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = AUTHED_PAGES[path].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        elif path in PAGES:
            body = PAGES[path].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        elif path in PUBLIC_APIS or path in AUTHED_APIS:
            if path in AUTHED_APIS and not authed:
                body = b'{"error": "unauthorized"}'
                self.send_response(401)
            else:
                body = json.dumps({"path": path}).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = b"<html><body>not found</body></html>"
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):  # keep the table's stdout clean
        pass


# ------------------------------------------------------------ ground truth

# Juice Shop: verified against master 2026-10-05.
#   routes — frontend/src/app/app.routing.ts (useHash: true), public split
#            against authed/admin views; "" is the app root.
#   apis   — service `host = '/api/…'` GET endpoints the curated routes fire
#            on load; POST-only endpoints excluded (crawl never submits).
JUICE_ROUTES_PUBLIC = [
    "", "search", "login", "register", "basket", "contact", "complain",
    "score-board", "recycle", "about", "privacy-policy", "forgot-password",
    "delivery-method",
]
JUICE_ROUTES_AUTHED = [
    "order-history", "change-password", "wallet", "data-export", "administration",
]
JUICE_ROUTES = JUICE_ROUTES_PUBLIC + JUICE_ROUTES_AUTHED
JUICE_APIS_PUBLIC = [
    "/api/Products", "/api/Feedbacks", "/rest/products/search", "/api/Challenges",
    "/rest/captcha", "/api/SecurityQuestions", "/rest/basket",
    "/rest/country-mapping", "/rest/user/whoami",
]
JUICE_APIS_AUTHED = [
    "/rest/order-history", "/rest/wallet/balance", "/api/BasketItems",
]
JUICE_APIS = JUICE_APIS_PUBLIC + JUICE_APIS_AUTHED
JUICE_URL = os.environ.get("ZERODOM_CRAWL_TARGET", "https://juice-shop.herokuapp.com/")


# ------------------------------------------------------------------ measuring

def _path(url: str) -> str:
    return urlsplit(url).path or "/"


def _juice_route(url: str) -> str | None:
    """Hash-SPA route key: fragment 'login' from https://host/#/login; root
    without fragment is home; a non-hash path is a raw link (extra, not a
    curated route) and returns None rather than faking a route match."""
    parts = urlsplit(url)
    if parts.fragment:
        return parts.fragment.lstrip("/")
    return "" if parts.path in ("", "/") else None


def _api_path(call: str) -> str:
    return _path(call.split(" ", 1)[1]) if " " in call else _path(call)


def _api_hit(api_path: str, truth: list[str]) -> bool:
    for t in truth:
        base = t.rstrip("/")
        if api_path == t or api_path.startswith(base + "/") or api_path.startswith(base + "?"):
            return True
    return False


def _summarize(records: list[dict], routes_truth: list[str], apis_truth: list[str],
               route_key, elapsed: float) -> dict:
    pages = [r for r in records if "error" not in r]
    errors = [r for r in records if "error" in r]
    blocked = [r for r in records if r.get("blocked")]
    routes = {route_key(r["url"]) for r in pages}
    routes.discard(None)
    apis = {_api_path(c) for r in pages for c in r.get("api_calls", [])}
    forms = sum(len(r.get("forms", [])) for r in pages)
    hit_r = [t for t in routes_truth if t in routes]
    hit_a = [t for t in apis_truth if _api_hit(t, apis)]
    missed = [t for t in routes_truth if t not in hit_r] + \
             [t for t in apis_truth if t not in hit_a]
    extras = (len(routes - set(routes_truth)) +
              sum(1 for a in apis if not _api_hit(a, apis_truth)))
    total = len(routes_truth) + len(apis_truth)
    return {
        "pages": len(pages),
        "routes": f"{len(hit_r)}/{len(routes_truth)}",
        "apis": f"{len(hit_a)}/{len(apis_truth)}",
        "forms": forms,
        "recall": f"{100.0 * (len(hit_r) + len(hit_a)) / total:.0f}%",
        "seconds": round(elapsed, 1),
        "missed": missed,
        "extras": extras,
        "errors": len(errors),
        "blocked": len(blocked),
        "note": "",
    }


def _skip(reason: str) -> dict:
    return {"pages": "—", "routes": "—", "apis": "—", "forms": "—",
            "recall": "—", "seconds": "—", "missed": [], "extras": 0,
            "errors": 0, "blocked": 0, "note": reason}


def _error(exc: Exception) -> dict:
    first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return _skip(f"error: {first[:90]}")


def _note(res: dict) -> str:
    parts = []
    if res.get("note"):
        parts.append(res["note"])
    if res.get("errors"):
        parts.append(f"{res['errors']} errors")
    if res.get("blocked"):
        parts.append(f"blocked x{res['blocked']}")
    if res.get("missed"):
        parts.append("missed: " + ", ".join(res["missed"]))
    if res.get("extras"):
        parts.append(f"+{res['extras']} extra")
    res["note"] = "; ".join(parts)
    return res


# ------------------------------------------------------------------- crawls

def _relay_endpoint() -> str | None:
    from zerodom.cli import _relay_reachable
    from zerodom.relay import DEFAULT_PORT

    return os.environ.get("ZERODOM_CDP_ENDPOINT") or (
        f"ws://127.0.0.1:{DEFAULT_PORT}/cdp/local" if _relay_reachable(DEFAULT_PORT) else None)


def _seed_session(root: str, seed: bool) -> str | None:
    """Put the fixture session cookie into the operator's Chrome (or take it
    back) over a short-lived connection of our own — crawl_site opens its own
    tab, so seeding cannot ride inside it. Returns a skip reason on failure.
    The operator's browser is never closed."""
    from playwright.sync_api import sync_playwright

    from zerodom.cli import _connect_relay

    # Probe before starting the driver: entering sync_playwright only to raise
    # SystemExit races the driver's own init and spews torn-down-task noise
    # (benchmark_session._fetch_relay precedent).
    if _relay_endpoint() is None:
        return ("no relay listening — run `zerodom relay` + the extension "
                "(or set ZERODOM_CDP_ENDPOINT)")
    try:
        with sync_playwright() as pw:
            browser = _connect_relay(pw)
            ctx = browser.contexts[0]
            if seed:
                ctx.add_cookies([{**SESSION_COOKIE, "url": root}])
            else:
                try:
                    ctx.clear_cookies(name=SESSION_COOKIE["name"], url=root)
                except Exception:
                    pass  # already gone / fixture never saw it
    except SystemExit as exc:
        return str(exc).removeprefix("zerodom: ")
    except Exception as exc:
        return f"relay unreachable: {str(exc)[:70]}"
    return None


def _crawl(url: str, *, relay: bool, seed_root: str | None, max_pages: int,
           routes_truth: list[str], apis_truth: list[str], route_key) -> dict:
    from zerodom.cli import crawl_site

    # Probe before starting the driver: crawl_site's relay branch raises
    # SystemExit inside its own sync_playwright() when no relay is listening,
    # which spews torn-down-task noise (seed legs probe in _seed_session).
    if relay and _relay_endpoint() is None:
        return _note(_skip("no relay listening — run `zerodom relay` + the "
                           "extension (or set ZERODOM_CDP_ENDPOINT)"))
    if relay and seed_root:
        if (reason := _seed_session(seed_root, True)) is not None:
            return _note(_skip(reason))
    t0 = time.monotonic()
    try:
        records = list(crawl_site(url, max_pages=max_pages, relay=relay))
    except SystemExit as exc:
        return _note(_skip(str(exc).removeprefix("zerodom: ")))
    except Exception as exc:
        return _note(_error(exc))
    finally:
        if relay and seed_root:
            _seed_session(seed_root, False)
    return _note(_summarize(records, routes_truth, apis_truth, route_key,
                            time.monotonic() - t0))


def _unreachable_note(url: str) -> str | None:
    """Precheck before spending browser minutes: an app that answers 5xx here
    gets an honest 'unreachable' row, not a crawl of its error page."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status >= 400:
                return f"target unreachable (HTTP {resp.status})"
    except Exception as exc:
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return f"target unreachable ({first[:80]})"
    return None


def main(argv: list[str]) -> int:
    # Coverage, not clearance: cap the ladder for every leg (§3.6 owns spend).
    os.environ.setdefault("ZERODOM_NO_SOLVER", "1")
    max_pages = int(os.environ.get("ZERODOM_CRAWL_MAX_PAGES", "25"))

    srv = HTTPServer(("127.0.0.1", 0), _Fixture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    root = f"http://127.0.0.1:{srv.server_port}"

    targets = [("fixture", f"{root}/", FIXTURE_ROUTES, FIXTURE_APIS, _path, root)]
    if os.environ.get("ZERODOM_E2E"):
        targets.append(("juice-shop", JUICE_URL, JUICE_ROUTES, JUICE_APIS,
                        _juice_route, None))

    rows: list[str] = [
        "| target | leg | pages | routes | apis | forms | recall | seconds | note |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]

    def add(target: str, leg: str, res: dict) -> None:
        line = (f"| {target} | {leg} | {res['pages']} | {res['routes']} | "
                f"{res['apis']} | {res['forms']} | {res['recall']} | "
                f"{res['seconds']} | {res['note']} |")
        rows.append(line)
        print(line, flush=True)

    have_relay = _relay_endpoint() is not None
    for name, url, r_truth, a_truth, key, seed_root in targets:
        # Fixture precheck: the server just started, skip the HTTP round-trip.
        down = None if name == "fixture" else _unreachable_note(url)
        for leg in ("headless", "relay"):
            if down:
                add(name, leg, _note(_skip(down)))
                continue
            add(name, leg, _crawl(
                url, relay=(leg == "relay"), seed_root=seed_root,
                max_pages=max_pages, routes_truth=r_truth, apis_truth=a_truth,
                route_key=key))

    srv.shutdown()

    notes = [
        "",
        "## Reading this",
        "",
        "- **recall** = (routes + API endpoints discovered) / ground truth. The",
        "  crawl is read-only, so POST-only endpoints are out of the truth set",
        "  and an endpoint counts only when a page load fires it — the number is",
        "  conservative by construction.",
        "- **fixture** is the deterministic session contrast: the guest nav has",
        "  no links to /account /orders /settings (they 302 to /login), so the",
        "  headless leg stalls at the public surface; the relay leg carries the",
        "  seeded zd_session cookie and reaches every authed route and endpoint.",
        "  Its one miss — /login — is real app behavior: the authed nav hides the",
        "  sign-in link. The cookie is removed after the relay leg.",
        "- **juice-shop** truth is source-derived (app.routing.ts + service GET",
        "  endpoints, verified 2026-10-05) and runs only under ZERODOM_E2E=1.",
        "  A down target is reported as unreachable rather than crawled as an",
        "  error page. Relay rows there use the operator's real session as-is.",
        "- **juice-shop's recall is honestly low**: its whole nav sits behind a",
        "  hamburger drawer that is closed at every tested viewport (1280, 1920,",
        "  2560 — all six nav anchors serialize data-zerodom-hidden, so the parser",
        "  correctly drops them: they are not clickable). Read-only BFS never",
        "  clicks (cli.crawl_site's contract), so the crawl maps the entry point",
        "  and stops at 0 in-scope links. Expanding drawer-gated IAs belongs to",
        "  the interaction layer (_act), not the crawler — a crawl-only ceiling,",
        "  not a claim that the surface was fully mapped.",
        "- **skipped relay**: no relay listening or no extension attached — this",
        "  script never spawns a relay and never closes the operator's browser",
        "  (crawl --relay opens and closes its own tab).",
        "- **WSTG mapping** (OWASP WSTG latest, 4.1 Information Gathering):",
        "  routes recall evidences 4.1.4 Attack Surface Identification and 4.1.10",
        "  Map Application Architecture; the forms column evidences 4.1.6",
        "  Identify Application Entry Points; api recall evidences 4.1.7 Map",
        "  Execution Paths Through Application. Search-engine recon, server and",
        "  framework fingerprinting, metafile and content review (4.1.1–3, 5,",
        "  8–9) are different techniques and are not claimed here.",
        f"- **max_pages** = {max_pages} (ZERODOM_CRAWL_MAX_PAGES raises the cap;",
        "  a bigger map needs a bigger budget on the live target).",
        "",
        "## Ground truth",
        "",
        f"- fixture routes: {', '.join(FIXTURE_ROUTES)}",
        f"- fixture apis: {', '.join(FIXTURE_APIS)}",
        f"- juice-shop routes ({len(JUICE_ROUTES)}): "
        + ", ".join(f"#/{r}" for r in JUICE_ROUTES_PUBLIC)
        + "; authed: " + ", ".join(f"#/{r}" for r in JUICE_ROUTES_AUTHED),
        f"- juice-shop apis ({len(JUICE_APIS)}): {', '.join(JUICE_APIS)}",
        "",
        "## Run",
        "",
        f"- date: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"- e2e: {'1 (juice-shop rows included)' if os.environ.get('ZERODOM_E2E') else '0 (fixture only)'}",
        f"- juice target: {JUICE_URL}",
        f"- relay endpoint: {'present' if have_relay else 'absent (relay rows skip)'}",
        f"- no_solver: {os.environ.get('ZERODOM_NO_SOLVER')}",
        "",
    ]
    out = [
        "# Crawl coverage: headless vs relay",
        "",
        "`uv run benchmarks/benchmark_crawl.py` writes this table;",
        "`ZERODOM_E2E=1` adds the Juice Shop target. One row per (target, leg).",
        "",
        *rows,
        *notes,
    ]
    (HERE / "CRAWL.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\nwrote {HERE / 'CRAWL.md'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
