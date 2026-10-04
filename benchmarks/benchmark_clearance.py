"""Challenge-clearance rate per rung, as a markdown table.

    uv run benchmarks/benchmark_clearance.py                # local fixture walls (always run)
    ZERODOM_E2E=1 uv run benchmarks/benchmark_clearance.py  # + live wall targets

Runs the Phase-0 ladder (zerodom.clearance) against a fixed wall-target list
and records, per target: cleared y/n, which rung cleared it, time-to-clear,
nodes parsed after, and whether metadata["blocked"] stayed set. The local
fixture walls stand up an HTTPServer in-process — one endpoint per rung plus
one honest miss — so the suite has a deterministic floor on every run and the
solver cell spends nothing (a fake local provider, never a real key). Live
targets (nowsecure.nl, the turnstile / reCAPTCHA / hCaptcha demos) are the
authorized wall list from HYPOTHESIS-ACK-PLAN §3.6 and are opt-in via
ZERODOM_E2E=1. Output: benchmarks/CLEARANCE.md.
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from zerodom import clearance
from zerodom.parser import ZeroDOMParser

HERE = pathlib.Path(__file__).parent

WALL = (
    '<html><head><script src="/turn.js"></script></head><body><form>'
    '<div class="cf-turnstile" data-sitekey="0xAAAAAAAA"></div>'
    '<input name="q"><button>Go</button></form></body></html>'
)
CLEAN = (
    "<html><title>App</title><body><form><input name=\"q\">"
    "<button>Go</button></form></body></html>"
)

# Per-path hit counters: fixture walls that clear on reload model the states
# the ladder actually meets (carried cookie / transient wall / solver cookie).
_HITS: dict[str, int] = {}


class _Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        # Hits are keyed by the full request target (query included) so rows
        # that share a path — the two /stealth rows — don't consume each
        # other's first-hit wall; routing ignores the query.
        _HITS[self.path] = _HITS.get(self.path, 0) + 1
        path = self.path.split("?")[0]
        cookie = self.headers.get("Cookie", "")
        if path == "/reused":
            # hit 1 is the wall the caller invoked us on; a later request that
            # carries the seeded clearance cookie gets the real page.
            ok = _HITS[self.path] > 1 and "cf_clearance=good" in cookie
        elif path == "/stealth":
            # the wall is transient: gone on any reload (rung 2's shape).
            ok = _HITS[self.path] > 1
        elif path == "/solver":
            ok = "cf_clearance=from-solver" in cookie
        else:  # /miss and /turn.js — nothing in this harness clears them
            ok = False
        body = (CLEAN if ok else WALL).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # keep the table's stdout clean
        pass


@contextmanager
def _env(**changes: str | None):
    """Set/remove env vars for the block, restored after (None removes)."""
    saved = {k: os.environ.get(k) for k in changes}
    for k, v in changes.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _outcome(result: dict | None, blocked, elapsed: float, notes: list[str]) -> dict:
    if result is not None:
        return {"outcome": "cleared", "rung": result["rung"],
                "seconds": result["seconds"]}
    return {"outcome": "blocked" if blocked else "no wall",
            "rung": "—", "seconds": round(elapsed, 2),
            "note": (notes[0][:60] if notes else "")}


def _row(target: str, transport: str, res: dict, html: str, url: str) -> str:
    graph = ZeroDOMParser(html, url).parse()
    meta = graph["metadata"]
    res = _outcome(res.get("result"), meta.get("blocked"),
                   res.get("elapsed", 0.0), res.get("notes", []))
    blocked = meta.get("blocked")
    blocked_col = blocked["kind"] if isinstance(blocked, dict) else ("false" if not blocked else blocked)
    cells = [target, transport, res["outcome"], res["rung"],
             f"{res['seconds']:.2f}", str(meta.get("total_interactive_nodes", 0)),
             str(blocked_col)]
    if res.get("note") and res["outcome"] != "cleared":
        cells.append(res["note"])
    return "| " + " | ".join(cells) + " |"


def _run_playwright(url: str, *, seed_cookie: str | None = None,
                    no_solver: bool = True, solver_url: str | None = None) -> tuple[dict, str, str]:
    """One ladder run on the Playwright transport; returns (raw, html, url)."""
    from playwright.sync_api import sync_playwright

    env = {"ZERODOM_NO_SOLVER": "1" if no_solver else None}
    if solver_url:
        # Force the custom-endpoint adapter regardless of any provider the
        # operator has in ~/.zerodom/config.json — this cell must stay local.
        env.update(ZERODOM_SOLVER_URL=solver_url, ZERODOM_SOLVER_PROVIDER="custom",
                   ZERODOM_SOLVER_KEY=None)
    with _env(**env):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context()
            if seed_cookie:
                ctx.add_cookies([{"name": "cf_clearance", "value": seed_cookie,
                                  "url": url}])
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            t0 = time.monotonic()
            result = clearance.attempt_clearance(
                page, None, clearance.make_policy(url=url, no_solver=no_solver))
            elapsed = time.monotonic() - t0
            notes = clearance.take_notes()
            html = page.content()
            final = page.url
            browser.close()
    return {"result": result, "elapsed": elapsed, "notes": notes}, html, final


def _run_pipe(url: str, *, no_solver: bool = True) -> tuple[dict, str, str]:
    """One ladder run on the cdp_pipe (stealth) transport via its choke point."""
    from zerodom import cdp_pipe

    cdp_pipe.find_chrome()  # raises CDPError when there is no binary to spawn
    with _env(ZERODOM_NO_SOLVER="1" if no_solver else None):
        t0 = time.monotonic()
        html, final = cdp_pipe.fetch(url)
        elapsed = time.monotonic() - t0
    result = clearance.take_cleared()
    notes = clearance.take_notes()
    return {"result": result, "elapsed": elapsed, "notes": notes}, html, final


def _fake_solver() -> tuple[HTTPServer, str]:
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = ('{"cookies":[{"name":"cf_clearance","value":"from-solver"}],'
                    '"mode":"cookie","cost":0.001}').encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/solve"


LIVE = [
    ("nowsecure.nl", "https://nowsecure.nl"),
    ("turnstile demo", "https://demo.turnstile.workers.dev"),
    ("recaptcha v2 demo", "https://recaptcha-demo.appspot.com/recaptcha-v2-checkbox-explicit.php"),
    ("hcaptcha demo", "https://accounts.hcaptcha.com/demo"),
]


def main(argv: list[str]) -> int:
    base = "http://127.0.0.1:{}"
    srv = HTTPServer(("127.0.0.1", 0), _Fixture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    root = base.format(srv.server_port)
    solver_srv, solver_url = _fake_solver()

    rows: list[str] = [
        "| target | transport | outcome | rung | seconds | nodes after | blocked | note |",
        "|---|---|---|---|---:|---:|---|---|",
    ]
    runs: list[str] = []  # one rung-or-miss per non-skipped row, for the summary

    def add(target: str, transport: str, raw: dict, html: str, url: str) -> None:
        line = _row(target, transport, raw, html, url)
        rows.append(line)
        runs.append(line.split("|")[4].strip())  # the rung/outcome cell
        print(line, flush=True)

    # -- local fixture floor (deterministic, no spend, always runs) ---------
    raw, html, url = _run_playwright(f"{root}/reused", seed_cookie="good")
    add("fixture/reused", "playwright", raw, html, url)

    try:
        raw, html, url = _run_pipe(f"{root}/stealth?row=pipe")
        add("fixture/stealth", "cdp_pipe", raw, html, url)
    except NotImplementedError:
        rows.append("| fixture/stealth | cdp_pipe | skipped | — | — | — | — |")
    except Exception as exc:  # no chrome binary / spawn failure — honest skip
        rows.append(f"| fixture/stealth | cdp_pipe | skipped | — | — | — | "
                    f"{type(exc).__name__} |")

    # Same wall, Playwright transport: rung 2 is a documented ceiling there
    # (Playwright's own driver is outside our control), so this must miss.
    raw, html, url = _run_playwright(f"{root}/stealth?row=pw")
    add("fixture/stealth", "playwright", raw, html, url)

    raw, html, url = _run_playwright(f"{root}/solver", no_solver=False,
                                     solver_url=solver_url)
    add("fixture/solver", "playwright", raw, html, url)

    raw, html, url = _run_playwright(f"{root}/miss")
    add("fixture/miss", "playwright", raw, html, url)

    srv.shutdown(); srv.server_close()
    solver_srv.shutdown(); solver_srv.server_close()

    # -- live wall list (authorized targets; ZERODOM_E2E=1 gates) -----------
    if os.environ.get("ZERODOM_E2E") == "1":
        for name, url in LIVE:
            try:
                raw, html, final = _run_playwright(url, no_solver=False)
            except Exception as exc:
                rows.append(f"| {name} | playwright | error | — | — | — | "
                            f"{type(exc).__name__} |")
                print(rows[-1], flush=True)
                continue
            add(name, "playwright", raw, html, final)
    else:
        print("live targets skipped (set ZERODOM_E2E=1 to include them)")

    # -- summary ------------------------------------------------------------
    total = len(runs)
    cleared = [r for r in runs if r in ("reused", "stealth", "solver", "human")]
    summary = ["| rung | cleared | rate |", "|---|---:|---:|"]
    for rung, label in (("reused", "1 reused"), ("stealth", "2 stealth"),
                        ("solver", "3 built-in solver"), ("human", "4 human")):
        n = sum(1 for r in runs if r == rung)
        summary.append(f"| {label} | {n}/{total} | {n / total:.1%} |"
                       if total else f"| {label} | 0/0 | — |")
    rate = f"{len(cleared) / total:.1%}" if total else "—"
    summary.append(f"| **overall cleared** | **{len(cleared)}/{total}** | **{rate}** |")

    header = (
        "# Challenge-clearance benchmark\n\n"
        "Generated by `uv run benchmarks/benchmark_clearance.py` "
        "(live targets: `ZERODOM_E2E=1 uv run benchmarks/benchmark_clearance.py`).\n\n"
        "`cleared` = the ladder got past the wall before the parse; `seconds` is "
        "ladder time-to-clear (or time-to-give-up); `blocked` is what "
        "`metadata[\"blocked\"]` still reported after the run. The solver cell "
        "runs against a local fake provider — no key, no spend. Rung 4 (human) "
        "only fires on the relay transport with an operator at the wheel, so an "
        "unattended run reports it as 0 by construction.\n\n"
    )
    out = HERE / "CLEARANCE.md"
    out.write_text(header + "\n".join(rows) + "\n\n## Per-rung clearance rate\n\n"
                   + "\n".join(summary) + "\n")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
