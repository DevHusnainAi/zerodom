"""Turn a web page into the list of things an agent can click, and act on it.

`zerodom <url>` prints the compact interaction graph (--find to search it).

Recon:
  inspect  <url>   the interaction graph + token savings (the default verb)
  crawl    <url>   map routes/forms/API calls as JSONL — plus the full callable
                   API surface (method + body shape) read from the page's JS,
                   over headless Chromium or your real Chrome (--relay)
  scan     <url>   match the page against client-side surface rules: DOM-XSS
                   sinks and leaked endpoints in the JS (--js), plus response-
                   header posture (CSP/CORS/cookie flags)

Access control:
  compare  <url>   fetch under two saved sessions and diff (cross-tenant IDOR)
  sweep    <url>   crawl as one identity, replay every URL across identities

Act / verify:
  repeat   <url>   send a request (optionally N times) with a chosen method/body
                   — a Repeater over the engagement session (--save HAR/.http)
  probe    <url>   replay with a tampered body (--tamper) and diff vs baseline —
                   mass-assignment / price / privilege tampering
  race     <url>   fire a concurrent burst (one body, or a mix of operations via
                   repeated --body) to flag a check-then-act race (TOCTOU);
                   --engine http2 multiplexes via httpx (pip install zerodom[race])

Also: audit (check a test suite's selectors), relay (bridge to real Chrome),
extension, setup, config. Shared flags: --proxy/--insecure (Burp/Caido),
--header, --storage-state (reuse a logged-in session)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

import tiktoken
from lxml import html as lxml_html

from . import clearance
from .parser import ZeroDOMParser, compact_line, find_nodes
from .playwright_wrapper import serialize

USER_AGENT = "Mozilla/5.0 (compatible; zerodom)"


def count_tokens(text: str) -> int:
    return len(tiktoken.get_encoding("cl100k_base").encode(text))


def _goto(page, url: str, transport: str = "playwright") -> None:
    """Navigate without hanging on a page that never goes network-idle.

    `wait_until="networkidle"` hangs the full 30s on any Turnstile/CF challenge
    or long-poll page (the widget keeps a socket open forever). Commit on DOM
    ready, then give the network a short bounded window to settle for SPAs and
    read whatever is there if it doesn't.

    Then the clearance ladder (choke point 1 of 3, see clearance.py): post-goto,
    pre-parse, rungs 1-4 attempt the wall. The result rides in the clearance
    module slot for whoever parses this fetch (inspect merges it into
    metadata["cleared"]); every fetch entry consumes the slot first so a
    plain-HTTP fetch in the same process can't inherit an earlier URL's result.
    transport="playwright": a CLI launch is nobody's live browser tab, so rung 4
    (human hand-off) stays off here; rung 2's ceiling on this transport is
    documented in clearance._rung_stealth. crawl --relay passes transport="relay"
    so the ladder treats the tab as the human's (rung 4 stays on).
    """
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass  # never idles (challenge widget / long-poll) — read the page as-is
    clearance.attempt_clearance(page, None, clearance.make_policy(url=url, transport=transport))


def _launch(pw, proxy: str | None, _retried: bool = False):
    try:
        return pw.chromium.launch(proxy={"server": proxy} if proxy else None)
    except Exception as exc:
        # The #1 first-run stumble is a browser command (--render/--screenshot/
        # --html/audit) before `playwright install chromium`. At an interactive
        # terminal, install it once and retry; piped/CI runs get the one-line hint
        # instead of a surprise 150MB download (and never Playwright's traceback).
        if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc):
            raise
        hint = "chromium isn't installed — run: playwright install chromium"
        if _retried or not (sys.stdin.isatty() and sys.stderr.isatty()):
            sys.exit(hint)
        print("Installing Chromium (~150MB, first run only)…", file=sys.stderr)
        import subprocess
        # sys.executable, not a bare `playwright`: under uvx/pipx the CLI isn't on PATH.
        if subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"]).returncode:
            sys.exit(hint)
        return _launch(pw, proxy, _retried=True)


def _context(browser, *, insecure, headers, storage_state):
    """A Playwright context carrying the engagement's headers and a reused,
    already-cleared session (Playwright storage_state JSON: cookies + origins)."""
    return browser.new_context(
        viewport={"width": 1280, "height": 900},
        ignore_https_errors=insecure,
        extra_http_headers=headers or None,
        storage_state=storage_state or None,
    )


def _cookies_from_state(storage_state: str | None) -> list[dict]:
    """The cookies out of a Playwright storage_state file, for the pipe/HTTP
    paths that can't consume the file natively (localStorage there is dropped —
    a cleared-session cookie like cf_clearance is what carries the challenge)."""
    if not storage_state:
        return []
    return json.loads(Path(storage_state).read_text(encoding="utf-8")).get("cookies", [])


class FetchError(Exception):
    """A page couldn't be fetched (bad URL, dead host, refused/timeout). Carries a
    one-line message so the CLI never shows a stranger a urllib/Playwright traceback
    for the everyday case of a typo'd or unreachable target."""


def fetch(
    url: str,
    render: bool,
    stealth: bool = False,
    proxy: str | None = None,
    insecure: bool = False,
    headers: dict | None = None,
    storage_state: str | None = None,
) -> tuple[str, str]:
    """Return (html, final_url). `render` runs a real browser for JS-heavy pages;
    `stealth` spawns a throwaway-profile Chrome over a CDP pipe (no port, no
    Playwright driver, nothing left on disk) — see cdp_pipe.py. `proxy` routes
    traffic through Burp/Caido; `insecure` trusts the proxy's own CA; `headers`
    adds a program's bypass header; `storage_state` reuses a cleared session.

    Raises FetchError (not a raw traceback) when the target can't be reached."""
    # Schemeless host → assume https. Everyone types `zerodom example.com`, and
    # `setup` itself suggests it; without this urllib raises "unknown url type".
    # scan resolves local files to file:// URIs upstream, so a schemeless arg
    # here is always a bare host.
    if "://" not in url:
        url = "https://" + url
    # Consume any ladder result from an earlier URL in this process — a
    # plain-HTTP fetch navigates nothing, so it must not leave the previous
    # URL's cleared/blocked record for inspect to pick up (stdin loops call
    # fetch per line, some --render, some not).
    clearance.take_cleared()
    clearance.take_notes()
    try:
        return _fetch(url, render, stealth, proxy, insecure, headers, storage_state)
    except (urllib.error.URLError, ValueError, OSError) as exc:
        # urllib: DNS/refused/timeout · ValueError: unknown url scheme (typo) ·
        # OSError: socket-level. SystemExit (missing chromium) is BaseException,
        # so it passes through untouched.
        reason = getattr(exc, "reason", exc)
        raise FetchError(f"could not fetch {url}: {reason}") from None
    except Exception as exc:  # Playwright nav failures (net::ERR_*, timeouts) only —
        if "playwright" not in type(exc).__module__:
            raise  # a real zerodom bug shouldn't be disguised as "could not fetch"
        first = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        raise FetchError(f"could not fetch {url}: {first}") from None


def _fetch(
    url: str,
    render: bool,
    stealth: bool = False,
    proxy: str | None = None,
    insecure: bool = False,
    headers: dict | None = None,
    storage_state: str | None = None,
) -> tuple[str, str]:
    if stealth:
        from .cdp_pipe import fetch as pipe_fetch

        return pipe_fetch(url, proxy=proxy, insecure=insecure,
                          headers=headers, cookies=_cookies_from_state(storage_state))
    if render:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = _launch(pw, proxy)
            page = _context(browser, insecure=insecure, headers=headers, storage_state=storage_state).new_page()
            _goto(page, url)
            html, final_url = serialize(page), page.url
            browser.close()
            return html, final_url

    r = _http_fetch(url, proxy, insecure, headers, storage_state)
    return r.body, r.url


class _Rate:
    """A monotonic-clock request pacer: `wait()` blocks so calls are spaced at
    least 1/rps apart. rps=0/None disables it. Shared across a sweep so the whole
    replay honours a program's rate limit, not just one URL's identities."""
    def __init__(self, rps: float | None):
        self.min = (1.0 / rps) if rps else 0.0
        self.t = 0.0

    def wait(self) -> None:
        if not self.min:
            return
        import time
        gap = self.t + self.min - time.monotonic()
        if gap > 0:
            time.sleep(gap)
        self.t = time.monotonic()


class HttpResp(NamedTuple):
    """One plain-HTTP response: status for compare's 403-vs-200, body for the
    graph, final URL after redirects, and the response headers for scan's posture
    rules (multiple Set-Cookie values are joined by newline, keys lowercased)."""
    status: int
    body: str
    url: str
    headers: dict[str, str]


def _flatten_headers(msg) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in msg.items():
        k = k.lower()
        out[k] = f"{out[k]}\n{v}" if k in out else v
    return out


def _http_fetch(url, proxy, insecure, headers, storage_state,
                method: str = "GET", body: str | None = None, cookie_jar=None) -> HttpResp:
    """Plain HTTP with the engagement's proxy/headers/cookies. `method`/`body`
    make it the shared sender for `repeat`/`race` (POST a JSON body); the default
    is the read that `scan`/`compare` have always used. `cookie_jar` (an
    http.cookiejar.CookieJar) makes the server's Set-Cookie carry across calls —
    repeat/race pass one jar so a server-issued session survives setup → burst,
    which urllib otherwise drops. Returns status, body, final URL and response
    headers (`compare` needs 403-vs-200; `scan` reads CSP/CORS/cookie posture)."""
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": USER_AGENT, **(headers or {})})
    if data is not None and not any(k.lower() == "content-type" for k in (headers or {})):
        req.add_header("Content-Type", "application/json")
    # One Cookie header with all pairs: add_header overwrites a repeat, so a
    # per-cookie loop would send only the last one (dropping Admin/role cookies
    # and silently breaking the compare IDOR diff).
    cookies = _cookies_from_state(storage_state)
    if cookies:
        req.add_header("Cookie", "; ".join(f"{c['name']}={c['value']}" for c in cookies))
    hs: list = []
    if cookie_jar is not None:
        hs.append(urllib.request.HTTPCookieProcessor(cookie_jar))
    if proxy:
        hs.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    if insecure:
        import ssl
        hs.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    opener = urllib.request.build_opener(*hs)
    try:
        with opener.open(req, timeout=30) as resp:
            text = resp.read().decode(resp.headers.get_content_charset() or "utf-8", "replace")
            return HttpResp(resp.status, text, resp.url, _flatten_headers(resp.headers))
    except urllib.error.HTTPError as e:  # 401/403/404 are the whole point of an IDOR check
        return HttpResp(e.code, e.read().decode("utf-8", "replace"), url, _flatten_headers(e.headers))


def _fetch_linked_scripts(
    html: str, base_url: str, *, proxy=None, insecure=False, headers=None,
    storage_state=None, cache: dict | None = None, max_scripts: int = 10,
    max_bytes: int = 512_000,
) -> list[str]:
    """Bodies of the page's same-origin `<script src>` bundles.

    jsintel stays network-free; this is where those bytes come from. Same-origin
    only — the app's own code, not CDN libraries (known and noisy). Per-script
    errors are swallowed (one dead asset must not fail a scan); `cache` (a dict
    keyed by URL) avoids refetching one bundle across many crawled pages."""
    try:
        doc = lxml_html.fromstring(html)
    except Exception:
        return []
    host = urlparse(base_url).netloc
    urls: list[str] = []
    seen: set[str] = set()
    for s in doc.iter("script"):
        src = s.get("src")
        if not src:
            continue
        absu = urljoin(base_url, src)
        if urlparse(absu).netloc != host or absu in seen:
            continue
        seen.add(absu)
        urls.append(absu)
    bodies: list[str] = []
    for u in urls[:max_scripts]:
        if cache is not None and u in cache:
            if cache[u]:
                bodies.append(cache[u])
            continue
        try:
            body = _http_fetch(u, proxy, insecure, headers, storage_state).body[:max_bytes]
        except Exception:
            body = ""
        if cache is not None:
            cache[u] = body
        if body:
            bodies.append(body)
    return bodies


def fetch_with_frames(url: str, proxy: str | None = None, insecure: bool = False,
                      headers: dict | None = None, storage_state: str | None = None):
    """Load in a real browser and read every readable iframe as well."""
    from playwright.sync_api import sync_playwright

    from .playwright_wrapper import ZeroDOM

    with sync_playwright() as pw:
        browser = _launch(pw, proxy)
        page = _context(browser, insecure=insecure, headers=headers, storage_state=storage_state).new_page()
        _goto(page, url)
        graph, html, final_url = ZeroDOM.from_page(page, frames=True), serialize(page), page.url
        browser.close()
    return graph, html, final_url


def capture(
    url: str, screenshot: str | None, html_path: str | None, frames: bool = False,
    proxy: str | None = None, insecure: bool = False,
    headers: dict | None = None, storage_state: str | None = None,
):
    """Load a URL once and write whichever visual artifacts were asked for.

    Measuring, annotating and screenshotting all need the same live page, so they
    share one browser session rather than one launch per artifact.
    """
    from playwright.sync_api import sync_playwright

    from . import report
    from .playwright_wrapper import ZeroDOM

    with sync_playwright() as pw:
        browser = _launch(pw, proxy)
        page = _context(browser, insecure=insecure, headers=headers, storage_state=storage_state).new_page()
        _goto(page, url)
        source, final_url = serialize(page), page.url
        graph = ZeroDOM.from_page(page, frames=frames)

        png, layout = report.screenshot(page, graph, screenshot)
        browser.close()

    located = len(layout["boxes"])
    notes = []
    if screenshot:
        notes.append(f"  Screenshot         {screenshot}  ({located}/{len(graph['nodes'])} located)")
    if html_path:
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(graph.to_html_report(png, layout))
        notes.append(f"  HTML report        {html_path}")
    return graph, source, final_url, "\n".join(notes)


_DEFAULT_DENY = "logout|sign-?out|delete|remove|destroy|revoke|deactivate|/api/.*(delete|remove)"


def crawl_plan(start_url: str, scope: set[str] | None = None, deny: str = _DEFAULT_DENY):
    """(in_scope, deny_re) for a BFS — shared by the CLI and MCP crawls so the
    two transports can never disagree about what's followable."""
    import re as _re

    deny_re = _re.compile(deny, _re.I)
    start_host = urlparse(start_url).netloc
    allow = {start_host} | (scope or set())

    def in_scope(u: str) -> bool:
        h = urlparse(u).netloc
        return h in allow and not deny_re.search(u)

    return in_scope, deny_re


def forms_of(doc, base: str) -> list[dict]:
    """Every `<form>` on the page as {action, method, inputs} — the payload an
    agent needs to re-submit one by hand."""
    out = []
    for f in doc.iter("form"):
        inputs = [
            {"name": i.get("name"), "type": (i.get("type") or i.tag)}
            for i in f.iter("input", "select", "textarea") if i.get("name")
        ]
        out.append({
            "action": urljoin(base, f.get("action") or base),
            "method": (f.get("method") or "get").upper(),
            "inputs": inputs,
        })
    return out


def crawl_record(html: str, final_url: str, api_calls: list[str], in_scope,
                 scripts: list[str] | None = None) -> dict:
    """One JSONL record for a crawled page: forms, in-scope links, fired API
    calls, callable JS endpoints, hidden fields, blocked. Shared byte-for-byte by
    the sync CLI crawl and the async MCP crawl — both transports emit the same
    record. `scripts` are the fetched external bundle bodies (the caller owns the
    network); inline scripts are read here. api_calls are what fired on load;
    js_endpoints are the full surface the JS can call (method + body shape)."""
    from .jsintel import _script_text, extract_endpoints

    graph = ZeroDOMParser(html, final_url).parse()
    try:
        doc = lxml_html.fromstring(html)
    except Exception:
        doc = None
    links = sorted({
        urljoin(final_url, n["href"]) for n in graph["nodes"]
        if n.get("href") and n["href"].split(":", 1)[0] not in ("javascript", "mailto", "tel")
    })
    js_eps: dict[tuple[str, str], dict] = {}
    for js in ([_script_text(doc)] if doc is not None else []) + (scripts or []):
        for e in extract_endpoints(js):
            js_eps[(e["method"], e["path"])] = {k: e[k] for k in ("method", "path", "body_keys")}
    record = {
        "url": final_url,
        "forms": forms_of(doc, final_url) if doc is not None else [],
        "api_calls": sorted(set(api_calls)),
        "js_endpoints": sorted(js_eps.values(), key=lambda e: (e["path"], e["method"])),
        "hidden_fields": [h["name"] for h in graph["metadata"].get("hidden_fields", [])],
        "links_in_scope": [l for l in links if in_scope(l)],
    }
    if blocked := graph["metadata"].get("blocked"):
        record["blocked"] = blocked["kind"]
    return record


def _connect_relay(pw):
    """Attach to the operator's real Chrome through the relay (connect_over_cdp),
    mirroring mcp_server._page(): a configured-but-unreachable endpoint fails
    closed — never a silent fallback to a fresh, unauthenticated browser."""
    from .relay import DEFAULT_PORT

    endpoint = os.environ.get("ZERODOM_CDP_ENDPOINT") or (
        f"ws://127.0.0.1:{DEFAULT_PORT}/cdp/local" if _relay_reachable(DEFAULT_PORT) else None)
    if not endpoint:
        raise SystemExit(
            "zerodom: --relay needs a relay endpoint — run `zerodom relay` with the "
            "ZeroDOM extension connected (or set ZERODOM_CDP_ENDPOINT) and retry")
    try:
        return pw.chromium.connect_over_cdp(endpoint, timeout=10000)
    except Exception as exc:
        raise SystemExit(f"zerodom: could not connect to the relay at {endpoint}: {exc}") from None


def crawl_site(
    start_url: str,
    *,
    scope: set[str] | None = None,
    max_pages: int = 40,
    deny: str = _DEFAULT_DENY,
    proxy: str | None = None,
    insecure: bool = False,
    headers: dict | None = None,
    storage_state: str | None = None,
    relay: bool = False,
):
    """Deep, authenticated, read-only recon of a real app: BFS-walk same-scope
    routes in a rendered browser (so JS SPAs actually load), and for each page
    record the forms, links, and the API calls it fired. This is the attack-
    surface map a hunter builds by hand — routes, params, endpoints — yielded one
    JSON object per page.

    Read-only by design: it navigates and reads, never submits a form or clicks a
    control matching `deny` (logout/delete/…), so it's safe to run unattended.

    Two transports. The default launches a fresh headless Chromium carrying the
    engagement's copied cookies (--proxy/--header/--storage-state). `relay=True`
    (`--relay`) attaches to the human's real, already-authenticated Chrome via
    the ZeroDOM relay — that browser *is* the session, so the engagement flags
    don't apply there, and their Chrome is never closed (our own tab is opened
    and closed instead; the page they're looking at is left alone). The BFS,
    scope check, deny regex, and per-page records are identical on both.
    """
    from playwright.sync_api import sync_playwright

    in_scope, _deny_re = crawl_plan(start_url, scope, deny)

    seen: set[str] = set()
    queue: list[str] = [start_url]
    with sync_playwright() as pw:
        own_tab = None
        if relay:
            browser = _connect_relay(pw)
            # Our own tab: never hijack the page the human is looking at.
            own_tab = browser.contexts[0].new_page()
            page = own_tab
        else:
            browser = _launch(pw, proxy)
            page = _context(browser, insecure=insecure, headers=headers,
                            storage_state=storage_state).new_page()
        try:
            # Desktop viewport: responsive sites expose their wide-layout navs
            # at 1920 where 1280 shows the collapsed form. juice-shop's
            # hamburger drawer stays closed at every width (verified 1280/1920/
            # 2560 — its nav anchors all serialize data-zerodom-hidden), so its
            # coverage number comes out of the interaction layer, not a width;
            # see benchmarks/CRAWL.md. fetch/inspect keep the browser default —
            # node counts there are token benchmarks, not coverage claims.
            page.set_viewport_size({"width": 1920, "height": 1080})
            # ponytail: crawl never clicks — a control that reveals links only
            # behind an interaction (hamburger menus, tabs, carousels) is not
            # expanded; mapping those needs the agent's _act layer, not BFS.
            api_calls: list[str] = []
            # XHR/fetch the page fires during a load = the app's real API surface.
            page.on("request", lambda r: api_calls.append(f"{r.method} {r.url}")
                    if r.resource_type in ("xhr", "fetch") else None)
            script_cache: dict[str, str] = {}  # one bundle fetched once across pages
            while queue and len(seen) < max_pages:
                url = queue.pop(0)
                if url in seen:
                    continue
                seen.add(url)
                api_calls.clear()
                try:
                    _goto(page, url, "relay" if relay else "playwright")
                    html, final = serialize(page), page.url
                except Exception as exc:
                    yield {"url": url, "error": str(exc)[:200]}
                    continue
                # Static-read the same-origin bundles for the full callable API
                # surface (methods/bodies the page never fires on a passive load).
                scripts = _fetch_linked_scripts(
                    html, final, proxy=proxy, insecure=insecure, headers=headers,
                    storage_state=storage_state, cache=script_cache)
                record = crawl_record(html, final, api_calls, in_scope, scripts=scripts)
                yield record
                if record.get("blocked"):
                    # A challenge interstitial's links are the wall's, not the
                    # app's — record the block and do not descend into it.
                    continue
                for l in record["links_in_scope"]:
                    if l not in seen and l not in queue:
                        queue.append(l)
        finally:
            if own_tab is not None:
                try:
                    own_tab.close()
                except Exception:
                    pass
            # Never browser.close() on a relay browser: it's the operator's
            # real Chrome (mcp_server._teardown_active_session precedent) —
            # leaving the with-block only stops our own driver connection.
            if not relay:
                browser.close()


def compare_identities(
    url: str,
    identities: list[tuple[str, str]],
    *,
    proxy: str | None = None,
    insecure: bool = False,
    headers: dict | None = None,
    rate: "_Rate | None" = None,
) -> dict:
    """Fetch one URL under each identity (a name + its storage_state cookies) and
    diff the responses — the cross-tenant IDOR primitive: does user A see user B's
    data? Byte-identical bodies across two identities on a per-user resource is
    the tell. Deterministic; interpretation stays with the human. `rate` paces the
    per-identity fetches to stay under a program's rate limit."""
    per: dict[str, dict] = {}
    bodies: dict[str, str] = {}
    for name, state in identities:
        if rate:
            rate.wait()
        status, body, final, _hdrs = _http_fetch(url, proxy, insecure, headers, state)
        bodies[name] = body
        graph = ZeroDOMParser(body, final).parse()
        per[name] = {
            "status": status,
            "bytes": len(body),
            "nodes": graph["metadata"]["total_interactive_nodes"],
            "sha256": hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:16],
            "_labels": {n.get("label", "") for n in graph["nodes"]},
        }

    # Any two identities returning the exact same body — the IDOR alarm.
    names = [n for n, _ in identities]
    identical: list[list[str]] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if per[a]["sha256"] == per[b]["sha256"]:
                identical.append([a, b])

    result: dict = {"url": url, "identities": {}, "identical_body_pairs": identical}
    if len(names) >= 2:
        a, b = names[0], names[1]
        result["only_" + a] = sorted(l for l in per[a]["_labels"] - per[b]["_labels"] if l)
        result["only_" + b] = sorted(l for l in per[b]["_labels"] - per[a]["_labels"] if l)
    if identical:
        result["note"] = ("byte-identical response under "
                          + " and ".join("/".join(p) for p in identical)
                          + " — a cross-tenant IDOR if this URL is a per-user resource")
    for name, d in per.items():
        d.pop("_labels", None)
        result["identities"][name] = d
    return result


_SWEEP_URL_CAP = 200  # ponytail: a flat cap on replayed URLs; raise/make a flag if a real engagement needs more


def _is_2xx(status: int) -> bool:
    return 200 <= status < 300


def _sweep_findings(urls, identities, *, proxy=None, insecure=False, headers=None, rate=None):
    """Replay each URL under every identity and keep only the deterministic
    access-control tells — the false-positive discipline is the point:

    - **cross-identity-identical-response** (high): a byte-identical 2xx body under
      two identities — the cross-tenant IDOR tell on a per-user resource.
    - **access-boundary-split** (medium): one identity authorized (2xx), another
      denied (401/403) — broken access control *if* the only difference between the
      identities is client-controllable (a cookie/header/param). Worded as a lead to
      confirm, never a confirmed bug, because the sweep can't know that on its own.

    A non-2xx identical body (two identical error pages) is NOT a finding.
    """
    from .surfaces import Finding

    findings = []
    for url in urls:
        try:
            result = compare_identities(url, identities, proxy=proxy, insecure=insecure,
                                        headers=headers, rate=rate)
        except (urllib.error.URLError, ValueError, OSError):
            continue  # one dead URL mid-sweep must not abort the whole run
        ids = result["identities"]
        status = {n: ids[n]["status"] for n in ids}
        flagged = False
        for a, b in result.get("identical_body_pairs", []):
            if _is_2xx(status[a]):  # identical error pages aren't a bug
                findings.append(Finding(
                    rule="cross-identity-identical-response", severity="high", url=url,
                    description=(f"Byte-identical response to {a} ({status[a]}) and {b} ({status[b]}) "
                                 "— cross-tenant IDOR if this is a per-user resource."),
                ))
                flagged = True
        if flagged:
            continue
        names = list(ids)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                sa, sb = status[a], status[b]
                if _is_2xx(sa) and sb in (401, 403):
                    ok, no = a, b
                elif _is_2xx(sb) and sa in (401, 403):
                    ok, no = b, a
                else:
                    continue
                findings.append(Finding(
                    rule="access-boundary-split", severity="medium", url=url,
                    description=(f"{ok} authorized ({status[ok]}) where {no} denied ({status[no]}) "
                                 "— broken access control if the only difference between these "
                                 "identities is client-controllable (cookie/header/param)."),
                ))
    return findings


def sweep_identities(start_url, identities, *, crawl_state=None, scope=None, max_pages=40,
                     deny=_DEFAULT_DENY, proxy=None, insecure=False, headers=None, rps=None):
    """The one-command cross-identity sweep: crawl the app as one identity to map
    its GET surface, then replay every in-scope URL under all identities and flag
    the access-control tells (`_sweep_findings`). "Two logins in, broken access
    control out." Read-only throughout — only GET api_calls are replayed (a POST/
    PUT/DELETE replay would change state) and destructive URLs are dropped. `rps`
    paces the replay to stay under a program's rate limit.

    # ponytail: v1 is GET-only + exact-URL dedup + a flat cap. Upgrades when
    # needed: path-template dedup (/orders/{id}), safe POST replay.
    """
    crawl_state = crawl_state or identities[0][1]  # discover the surface as the first identity
    in_scope, deny_re = crawl_plan(start_url, scope, deny)
    urls, seen = [], set()
    for rec in crawl_site(start_url, scope=scope, max_pages=max_pages, deny=deny, proxy=proxy,
                          insecure=insecure, headers=headers, storage_state=crawl_state):
        if rec.get("error") or rec.get("blocked"):
            continue
        cands = [rec["url"], *rec.get("links_in_scope", [])]
        for call in rec.get("api_calls", []):
            method, _, u = call.partition(" ")
            if method.upper() == "GET" and u:
                cands.append(u)
        for u in cands:
            if u in seen or not in_scope(u) or deny_re.search(u):
                continue
            seen.add(u)
            urls.append(u)
    return _sweep_findings(urls[:_SWEEP_URL_CAP], identities,
                           proxy=proxy, insecure=insecure, headers=headers, rate=_Rate(rps))


def emit_jsonl(graph) -> None:
    """The flattened 1D node array as clean JSONL: one page header line, then one
    node per line. Greppable, streamable, zero pretty-printing overhead."""
    meta = graph["metadata"]
    out = sys.stdout
    out.write(json.dumps({"url": meta["url"], "nodes": len(graph["nodes"])}) + "\n")
    for node in graph["nodes"]:
        out.write(json.dumps(node, ensure_ascii=False) + "\n")
    out.flush()


def _banner(stream) -> None:
    """One-line star/Discord nudge for humans only. Gated on the stream being a
    real TTY, so a pipe, redirect or `| jq` never sees it (keeps machine output
    clean); `ZERODOM_NO_BANNER=1` silences it. ponytail: no phone-home, no
    first-run state file — static links, printed, done."""
    if os.environ.get("ZERODOM_NO_BANNER") or not getattr(stream, "isatty", lambda: False)():
        return
    print("  ★ github.com/DevHusnainAi/zerodom   ·   discord.gg/tVRznUb5Ez",
          file=stream)


def inspect(
    url: str,
    render: bool = False,
    as_json: bool = False,
    screenshot: str | None = None,
    html_path: str | None = None,
    find: str | None = None,
    frames: bool = False,
    stealth: bool = False,
    as_pipe: bool = False,
    proxy: str | None = None,
    insecure: bool = False,
    headers: dict | None = None,
    storage_state: str | None = None,
) -> int:
    # --frames traverses iframes via Playwright; --stealth is the CDP-pipe path
    # that has no frame traversal. They can't compose, so say so instead of
    # silently ignoring --stealth (the frames branch used to win quietly).
    if frames and stealth:
        raise SystemExit("zerodom: --frames and --stealth can't be combined "
                         "(--frames needs the Playwright render path; --stealth is the CDP pipe)")

    # A local path is a perfectly good thing to inspect; both fetchers need file://.
    if "://" not in url and Path(url).exists():
        url = Path(url).resolve().as_uri()

    extra = ""
    try:
        if screenshot or html_path:
            # One browser session for the graph and every artifact asked for.
            graph, html, final_url, extra = capture(
                url, screenshot, html_path, frames, proxy, insecure, headers, storage_state)
        elif frames:
            # Frames only exist in a live browser, so this is a --render superset.
            graph, html, final_url = fetch_with_frames(url, proxy, insecure, headers, storage_state)
        else:
            html, final_url = fetch(url, render, stealth, proxy, insecure, headers, storage_state)
            graph = ZeroDOMParser(html, final_url).parse()
    except FetchError as exc:
        print(f"zerodom: {exc}", file=sys.stderr)  # dead host / typo — one line, no traceback
        return 1
    except Exception as exc:  # nav failure on the render/frames path (no FetchError wrap there)
        if "playwright" not in type(exc).__module__:
            raise
        print(f"zerodom: could not fetch {url}: {str(exc).splitlines()[0]}", file=sys.stderr)
        return 1

    # Whatever the ladder did during this fetch becomes metadata["cleared"]
    # beside the parser's metadata["blocked"], and the human-facing notes
    # (rung 3/4) ride along for the report below.
    if cleared := clearance.take_cleared():
        graph["metadata"]["cleared"] = cleared
    ladder_notes = clearance.take_notes()

    if as_pipe:  # machine path: JSONL to stdout, no token report
        emit_jsonl(graph)
        return 0

    raw_tokens = count_tokens(html)
    # Measure what a model would actually be sent: compact JSON, or the text DSL.
    payload = graph.to_json(indent=None) if as_json else graph.to_compact_text()
    graph_tokens = count_tokens(payload)
    savings = (1 - graph_tokens / raw_tokens) * 100 if raw_tokens else 0.0

    if find:
        hits = find_nodes(graph["nodes"], find)
        print("\n".join(compact_line(node) for node in hits) if hits
              else f"No node matches {find!r} among {len(graph['nodes'])} nodes.")
    else:
        print(graph.to_json() if as_json else graph.to_compact_text())
    # --json is a machine payload; keep stdout pure JSON so `zerodom … --json | jq`
    # works, and send the human token report to stderr instead.
    report = sys.stderr if as_json else sys.stdout
    print(f"\n{'─' * 52}", file=report)
    print(f"  URL                {final_url}", file=report)
    print(f"  Interactive nodes  {graph['metadata']['total_interactive_nodes']}", file=report)
    print(f"  Parsing latency    {graph['metadata']['parsing_latency_ms']} ms", file=report)
    print(f"  Raw DOM tokens     {raw_tokens:,}", file=report)
    print(f"  ZeroDOM tokens     {graph_tokens:,}  ({'json' if as_json else 'compact text'})", file=report)
    print(f"  Token savings      {savings:.1f}%", file=report)
    if blocked := graph["metadata"].get("blocked"):
        print(f"  Blocked            {blocked['kind']} challenge — ladder did not clear it "
              f"(reuse a cleared session with --storage-state, retry --stealth, set a solver "
              f"provider, or drive it in relay mode; --no-solver forces rung 3 off)",
              file=report)
    elif cleared := graph["metadata"].get("cleared"):
        print(f"  Cleared            {cleared['wall_kind']} via rung {cleared['rung']} "
              f"({cleared['attempts']} attempt{'s' if cleared['attempts'] != 1 else ''}, "
              f"{cleared['seconds']}s)", file=report)
    for note in ladder_notes:
        print(f"  Note               {note}", file=report)
    if extra:
        print(extra, file=report)
    print("─" * 52, file=report)
    _banner(report)
    return 0


def _stdin_urls():
    """URLs piped on stdin, one per line; blank lines and `#` comments skipped."""
    for line in sys.stdin:
        line = line.strip()
        if line and not line.startswith("#"):
            yield line


def scan_targets(args) -> int:
    """Run the surface ruleset over each target, stream findings as JSONL.

    Exit is non-zero only under --fail-on-finding, so a plain scan stays
    pipe-friendly (`... | jq` sees a clean stream, shell sees success).
    """
    from .surfaces import csrf_posture, evaluate, evaluate_headers, load_header_rules, load_rules

    try:
        rules = load_rules(args.rules)  # loads once; raises loudly on a bad ruleset
        header_rules = load_header_rules(args.rules)
    except Exception as exc:
        # A --rules file is config: a missing file, bad YAML, or an invalid rule
        # (load_rules' own ValueErrors are already user-readable) is a clean exit,
        # not a traceback dumped at someone writing their first ruleset.
        sys.exit(f"zerodom: bad rules file: {exc}")
    urls = _stdin_urls() if args.url == "-" else [args.url]
    as_md = getattr(args, "fmt", "jsonl") == "md"
    any_finding = False
    collected: list = []  # md buffers (a table needs every row); jsonl streams
    for url in urls:
        target = url
        if "://" not in target and Path(target).exists():
            target = Path(target).resolve().as_uri()
        try:
            html, final_url = fetch(target, args.render, args.stealth, args.proxy,
                                    args.insecure, _parse_headers(args.headers), args.storage_state)
        except FetchError as exc:
            # A dead host mid-sweep must not abort the batch — skip it, keep scanning.
            print(f"zerodom: {exc}", file=sys.stderr)
            continue
        graph = ZeroDOMParser(html, final_url).parse()
        findings = list(evaluate(graph, rules))
        hdrs = _parse_headers(args.headers)
        post_surf: set[str] = set()
        if args.js:
            from .jsintel import extract_endpoints, scan_js
            scripts = _fetch_linked_scripts(
                html, final_url, proxy=args.proxy, insecure=args.insecure,
                headers=hdrs, storage_state=args.storage_state)
            findings += scan_js(html, final_url, scripts=scripts)
            post_surf |= {e["path"] for s in scripts for e in extract_endpoints(s) if e["method"] != "GET"}
        # Server-rendered <form method=post> is a state-changing surface too, so a
        # classic CSRF-able form is caught even without JS endpoints.
        try:
            post_surf |= {f["action"] for f in forms_of(lxml_html.fromstring(html), final_url)
                          if f["method"] == "POST"}
        except Exception:
            pass
        post_eps = sorted(post_surf)
        # Response-header posture + CSRF correlation: one plain-HTTP read of the
        # document response (where CSP/CORS/Set-Cookie live, identical under
        # --render) for http(s).
        if target.startswith(("http://", "https://")):
            try:
                resp = _http_fetch(target, args.proxy, args.insecure, hdrs, args.storage_state)
                findings += evaluate_headers(resp.headers, final_url, header_rules)
                hidden_blob = " ".join(h.get("name", "") for h in graph["metadata"].get("hidden_fields", []))
                if csrf := csrf_posture(resp.headers, post_eps, hidden_blob, final_url):
                    findings.append(csrf)
            except Exception:
                pass  # a header read that fails must not abort the scan
        for finding in findings:
            any_finding = True
            if as_md:
                collected.append(finding)
            else:
                sys.stdout.write(json.dumps(finding.to_dict(), ensure_ascii=False) + "\n")
    if as_md:
        sys.stdout.write(_findings_markdown(collected))
    sys.stdout.flush()
    return 1 if (any_finding and args.fail_on_finding) else 0


def _findings_markdown(findings: list) -> str:
    """Findings as a GitHub-flavoured Markdown table — pastes straight into a
    HackerOne report, GitHub issue or Jira ticket. Pipes/newlines in a cell are
    escaped so one description can't break the table."""
    if not findings:
        return "_No findings._\n"
    def cell(v: object) -> str:
        return str(v or "").replace("|", "\\|").replace("\n", " ").strip()
    dicts = [f.to_dict() for f in findings]
    # Drop columns that are empty for every finding — sweep findings have no
    # node/label/selector, so keeping those would print dead "| |" columns.
    cols = [c for c in ("severity", "rule", "description", "label", "selector", "url")
            if any(cell(d.get(c)) for d in dicts)]
    rows = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for d in dicts:
        rows.append("| " + " | ".join(cell(d.get(c)) for c in cols) + " |")
    return "\n".join(rows) + "\n"


def extension_dir() -> Path:
    """The unpacked extension: bundled in the wheel, or the repo copy in a source checkout."""
    here = Path(__file__).resolve().parent
    for d in (here / "extension", here.parent / "extension"):
        if (d / "manifest.json").is_file():
            return d
    raise SystemExit("zerodom: extension files not found in this install")


def _add_fetch_flags(p) -> None:
    """Engagement fetch options shared by inspect and scan. `ZERODOM_PROXY`
    supplies the proxy default so a hunter can export it once per engagement."""
    p.add_argument(
        "--proxy", metavar="URL", default=os.environ.get("ZERODOM_PROXY"),
        help="route traffic through an intercepting proxy, e.g. http://127.0.0.1:8080 "
             "(Burp/Caido); defaults to $ZERODOM_PROXY",
    )
    p.add_argument(
        "--insecure", action="store_true",
        help="trust the proxy's own TLS CA (ignore certificate errors) — needed for Burp/Caido",
    )
    p.add_argument(
        "--header", metavar="'K: V'", action="append", dest="headers",
        help="extra request header, repeatable — e.g. a program's WAF bypass token",
    )
    p.add_argument(
        "--storage-state", metavar="STATE.json", dest="storage_state",
        help="reuse a Playwright storage_state (cookies + localStorage) — carries a "
             "human-cleared session, e.g. a cf_clearance cookie, into automated reads",
    )
    p.add_argument(
        "--no-solver", action="store_true",
        help="disable rung 3 (the built-in solver) of the challenge-clearance "
             "ladder for this run — env ZERODOM_NO_SOLVER=1 does the same host-wide",
    )


def _parse_headers(pairs: list | None) -> dict:
    """`--header 'K: V'` values → {K: V}. A header without a colon is a user error."""
    out = {}
    for raw in pairs or []:
        if ":" not in raw:
            raise SystemExit(f"zerodom: --header needs 'Key: Value', got {raw!r}")
        k, v = raw.split(":", 1)
        out[k.strip()] = v.strip()
    return out


_CONFIG_KEYS = ("solver.provider", "solver.key", "solver.url")


def _mask_config_value(key: str, value: str) -> str:
    """Secrets echo masked — set/get/list all print, and stdout is history."""
    if not key.endswith(".key"):
        return value
    if len(value) > 8:
        return f"{value[:4]}…{value[-4:]} (masked)"
    return "(masked)"


def config_cmd(args) -> int:
    """`zerodom config set/get/list` — the in-product half of solver setup
    (T0.1): values land in ~/.zerodom/config.json (0600), env vars beat them,
    and the ladder reads the result through solvers.get_solver()."""
    from .solvers import CONFIG_PATH, load_config, save_config

    if args.config_action == "list":
        cfg = load_config()
        found = False
        for key in _CONFIG_KEYS:
            section, name = key.split(".")
            if isinstance(cfg.get(section), dict) and name in cfg[section]:
                found = True
                print(f"{key} = {_mask_config_value(key, str(cfg[section][name]))}")
        if not found:
            print(f"(nothing stored in {CONFIG_PATH})")
        return 0
    if args.key not in _CONFIG_KEYS:
        raise SystemExit(f"zerodom: unknown setting {args.key!r} "
                         f"(known: {', '.join(_CONFIG_KEYS)})")
    if args.config_action == "set":
        cfg = load_config()
        section, name = args.key.split(".")
        cfg.setdefault(section, {})[name] = args.value
        save_config(cfg)
        print(f"{args.key} = {_mask_config_value(args.key, args.value)}  ({CONFIG_PATH})")
        return 0
    cfg = load_config()
    node: object = cfg
    for part in args.key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise SystemExit(f"zerodom: {args.key} is not set")
        node = node[part]
    print(f"{args.key} = {_mask_config_value(args.key, str(node))}")
    return 0


def _parse_identities(pairs: list | None) -> list[tuple[str, str]]:
    """`--as NAME=STATE.json` values → [(name, path)]. Needs at least two to diff."""
    ids: list[tuple[str, str]] = []
    for raw in pairs or []:
        if "=" not in raw:
            raise SystemExit(f"zerodom: --as needs NAME=STATE.json, got {raw!r}")
        name, path = raw.split("=", 1)
        if not Path(path).is_file():
            raise SystemExit(f"zerodom: identity {name!r} state file not found: {path}")
        ids.append((name.strip(), path.strip()))
    if len(ids) < 2:
        raise SystemExit("zerodom: needs at least two --as identities to diff")
    return ids


def _chromium_installed() -> bool:
    """True if Playwright's Chromium is on disk. Checks the browser cache rather
    than launching the driver (which prints asyncio teardown noise). Honors
    PLAYWRIGHT_BROWSERS_PATH; a hint, not a guarantee — _launch is the real check."""
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    roots = [Path(base)] if base else [
        Path.home() / ".cache" / "ms-playwright",              # linux
        Path.home() / "Library" / "Caches" / "ms-playwright",  # macOS
        Path.home() / "AppData" / "Local" / "ms-playwright",   # windows
    ]
    return any(r.is_dir() and any(r.glob("chromium-*")) for r in roots)


def _relay_reachable(port: int = 8765) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def setup(_args=None) -> int:
    """Guided first-run: sequences the pieces a stranger otherwise has to find
    across the README (Chromium, the unpacked extension, the MCP config, the
    relay). Print-and-guide, no account, no telemetry — nothing leaves the box."""
    out = sys.stdout
    print("ZeroDOM setup — local, no account, nothing leaves your machine.\n", file=out)

    # 1. Chromium (only needed for --render / --frames / the MCP driving path).
    if _chromium_installed():
        print("[1/4] Chromium: installed ✓", file=out)
    else:
        print("[1/4] Chromium: not installed — needed for --render and the MCP driving path.\n"
              "      Run: playwright install chromium", file=out)

    # 2. The unpacked Chrome extension (drives your real, logged-in browser).
    try:
        ext = extension_dir()
        print(f"\n[2/4] Chrome extension (for driving your logged-in browser):\n"
              f"      1. Open chrome://extensions\n"
              f"      2. Enable 'Developer mode' (top-right)\n"
              f"      3. 'Load unpacked' → select: {ext}\n"
              f"      The extension auto-connects to the relay — nothing to configure.", file=out)
    except SystemExit as exc:
        print(f"\n[2/4] Chrome extension: {exc}", file=out)

    # 3. MCP config for Claude Desktop / Cursor.
    cfg = (
        '{\n'
        '  "mcpServers": {\n'
        '    "zerodom": {\n'
        '      "command": "uvx",\n'
        '      "args": ["--from", "zerodom", "zerodom-mcp"]\n'
        '    }\n'
        '  }\n'
        '}'
    )
    print("\n[3/4] MCP server — add this to your client config:\n" + cfg, file=out)
    print("      Claude Desktop: ~/.config/Claude/claude_desktop_config.json "
          "(macOS: ~/Library/Application Support/Claude/…)\n"
          "      Cursor: ~/.cursor/mcp.json", file=out)

    # 4. Relay reachability.
    if _relay_reachable():
        print("\n[4/4] Relay: reachable on :8765 ✓", file=out)
    else:
        print("\n[4/4] Relay: not running. The MCP server auto-spawns it, or start it yourself:\n"
              "      zerodom relay", file=out)

    print("\nDone. Quick test (no browser needed): zerodom example.com", file=out)
    return 0


def _har_entry(method, url, req_headers, req_body, resp) -> dict:
    """One HAR 1.2 entry from a request/response pair — the format Burp/Caido and
    browsers import. Response headers arrive lowercased with multi-value Set-Cookie
    newline-joined (as _http_fetch returns), split back out here."""
    import datetime

    def _hlist(h):
        out = []
        for k, v in (h or {}).items():
            for part in str(v).split("\n"):
                out.append({"name": k, "value": part})
        return out

    req = {"method": method, "url": url, "httpVersion": "HTTP/1.1",
           "headers": _hlist(req_headers), "queryString": [], "cookies": [],
           "headersSize": -1, "bodySize": len(req_body or "")}
    if req_body is not None:
        req["postData"] = {"mimeType": "application/json", "text": req_body}
    return {
        "startedDateTime": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "time": 0,
        "request": req,
        "response": {"status": resp.status, "statusText": "", "httpVersion": "HTTP/1.1",
                     "headers": _hlist(resp.headers), "cookies": [],
                     "content": {"size": len(resp.body), "mimeType":
                                 resp.headers.get("content-type", ""), "text": resp.body},
                     "headersSize": -1, "bodySize": len(resp.body), "redirectURL": ""},
        "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0},
    }


def _write_evidence(path: str, exchanges: list[dict]) -> None:
    """Write captured exchanges to `path`. `.http` → a human-readable request/
    response log; anything else (default `.har`) → a HAR file for Burp/Caido/browser
    import. Each exchange: {method,url,req_headers,req_body,resp}."""
    from . import __version__ as _ver

    if path.endswith(".http"):
        blocks = []
        for i, x in enumerate(exchanges, 1):
            r = x["resp"]
            req = [f"{x['method']} {x['url']} HTTP/1.1"]
            req += [f"{k}: {v}" for k, v in (x["req_headers"] or {}).items()]
            req_txt = "\r\n".join(req) + "\r\n\r\n" + (x["req_body"] or "")
            res = [f"HTTP/1.1 {r.status}"]
            res += [f"{k}: {v}" for k, v in (r.headers or {}).items()]
            res_txt = "\r\n".join(res) + "\r\n\r\n" + r.body
            blocks.append(f"### exchange {i} — request\n{req_txt}\n\n### exchange {i} — response\n{res_txt}")
        Path(path).write_text("\n\n".join(blocks), encoding="utf-8")
        return
    har = {"log": {"version": "1.2", "creator": {"name": "zerodom", "version": _ver},
                   "entries": [_har_entry(x["method"], x["url"], x["req_headers"],
                                          x["req_body"], x["resp"]) for x in exchanges]}}
    Path(path).write_text(json.dumps(har, ensure_ascii=False, indent=2), encoding="utf-8")


def _pp_body(text: str, limit: int = 1000):
    """Parse a response body as JSON for a clean record, else a trimmed string."""
    try:
        return json.loads(text)
    except Exception:
        return text if len(text) <= limit else text[:limit] + "…"


def _cookie_header(jar, storage_state, url: str) -> str:
    """The Cookie header for `url`: the shared jar's cookies (a server-issued
    session from setup) plus any static --storage-state cookies."""
    req = urllib.request.Request(url)
    if jar is not None:
        jar.add_cookie_header(req)
    parts = [req.get_header("Cookie")] if req.get_header("Cookie") else []
    parts += [f"{c['name']}={c['value']}" for c in _cookies_from_state(storage_state)]
    return "; ".join(p for p in parts if p)


def _race_burst(url, specs, headers, *, jar, storage_state, insecure) -> list[int]:
    """Fire the burst with last-byte synchronisation (the HTTP/1.1 single-packet
    technique). `specs` is a list of (method, body) — identical for a single-action
    race, or a mix of operations that hammer one shared record (e.g. two coupon
    codes on one cart), which widens the read-modify-write window. Each connection
    sends its whole request except the final byte; then one thread releases every
    final byte back-to-back, so the requests complete within microseconds of each
    other — tight enough to land several inside a check-then-act window. Returns the
    status per spec, in order (-1 on a send/parse error).
    ponytail: one connection per request; for the very tightest windows the http2
    engine (zerodom[race]) multiplexes on one connection instead."""
    import socket
    import ssl as sslmod
    import threading

    pu = urlparse(url)
    host = pu.hostname
    port = pu.port or (443 if pu.scheme == "https" else 80)
    path = pu.path + (f"?{pu.query}" if pu.query else "")
    cookie = _cookie_header(jar, storage_state, url)
    hset = {"host", "user-agent", "accept", "connection", "content-length", "content-type", "cookie"}

    def _raw(method: str, body: str | None) -> bytes:
        payload = body.encode("utf-8") if body is not None else b""
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", f"User-Agent: {USER_AGENT}",
                 "Accept: */*", "Connection: close"]
        for k, v in (headers or {}).items():
            if k.lower() not in hset:
                lines.append(f"{k}: {v}")
        if body is not None and not any(k.lower() == "content-type" for k in (headers or {})):
            lines.append("Content-Type: application/json")
        if cookie:
            lines.append(f"Cookie: {cookie}")
        if method not in ("GET", "HEAD") or payload:
            lines.append(f"Content-Length: {len(payload)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + payload

    raws = [_raw(m, b) for m, b in specs]
    ctx = (sslmod._create_unverified_context() if insecure
           else sslmod.create_default_context()) if pu.scheme == "https" else None

    # Phase 1: open and warm every connection in parallel (handshake + all but the
    # last byte). Phase 2: one thread sends every final byte back-to-back — no
    # per-send GIL/barrier jitter. Phase 3: read each status, aligned to specs.
    n = len(specs)
    socks: list = [None] * n

    def _warm(i: int):
        try:
            s = socket.create_connection((host, port), timeout=30)
            if ctx is not None:
                s = ctx.wrap_socket(s, server_hostname=host)
            s.sendall(raws[i][:-1])
            socks[i] = s
        except Exception:
            socks[i] = None

    warmers = [threading.Thread(target=_warm, args=(i,)) for i in range(n)]
    for t in warmers:
        t.start()
    for t in warmers:
        t.join()

    for i, s in enumerate(socks):   # the single-packet moment: final bytes, back-to-back
        if s is not None:
            try:
                s.send(raws[i][-1:])
            except Exception:
                pass

    results: list[int] = [-1] * n
    for i, s in enumerate(socks):
        if s is None:
            continue
        try:
            buf = s.recv(4096)
            results[i] = int(buf.split(b" ", 2)[1]) if buf.startswith(b"HTTP/") else -1
        except Exception:
            results[i] = -1
        finally:
            try:
                s.close()
            except Exception:
                pass
    return results


def _run_steps(steps: list[dict], base_url: str, *, proxy, insecure, headers,
               storage_state, cookie_jar=None) -> None:
    """Run a list of prep requests (a --setup file) in order, to reach the state a
    race needs. Each step: {url|path, method?, body?}; a path is joined onto the
    target's origin. A shared `cookie_jar` carries a server-issued session from one
    step to the next (and into the burst)."""
    for step in steps:
        u = step.get("url") or urljoin(base_url, step["path"])
        body = step.get("body")
        if body is not None and not isinstance(body, str):
            body = json.dumps(body)
        _http_fetch(u, proxy, insecure, headers, storage_state,
                    method=step.get("method", "GET").upper(), body=body, cookie_jar=cookie_jar)


def probe_finding(injected: dict, base_body: str | None, tamp_status: int, tamp_body: str,
                  method: str, url: str):
    """Mass-assignment tell, shared by CLI `probe` and MCP `zerodom_probe`: an
    injected field accepted (2xx) and reflected in the tampered response but absent
    from the baseline. Returns a Finding or None."""
    from .surfaces import Finding

    if not injected:
        return None
    reflected = [k for k, v in injected.items()
                 if str(v) in (tamp_body or "") and (base_body is None or str(v) not in base_body)]
    if 200 <= tamp_status < 300 and reflected:
        return Finding(
            "mass-assignment", "high",
            f"injected field(s) {reflected} were accepted (HTTP {tamp_status}) and reflected by "
            f"{method} {url} but absent from the baseline response — possible mass assignment "
            f"(privilege/price/ownership tampering); confirm the server-side effect",
            url)
    return None


def probe_cmd(args) -> int:
    """Replay a (crawl-discovered) endpoint with a tampered body and diff it against
    the baseline — the mass-assignment / price- / privilege-tampering check that
    read-only recon can't make. `--tamper '{"role":"admin"}'` is merged over
    `--body`; if an injected field is accepted (2xx) and shows up in the tampered
    response but not the baseline, that is flagged. `--setup` reaches the pre-state;
    `--save` writes both exchanges as evidence. (Cross-identity write-IDOR is
    `zerodom compare <url> --method POST --body … --as …`.)"""
    import http.cookiejar

    from .surfaces import Finding

    hdrs = _parse_headers(args.headers)
    jar = http.cookiejar.CookieJar()
    method = args.method.upper()

    tampered = args.body
    injected: dict = {}
    if args.tamper:
        injected = json.loads(args.tamper)
        merged = (json.loads(args.body) if args.body else {})
        merged.update(injected)
        tampered = json.dumps(merged)

    if args.setup:
        _run_steps(json.loads(Path(args.setup).read_text(encoding="utf-8")), args.url,
                   proxy=args.proxy, insecure=args.insecure, headers=hdrs,
                   storage_state=args.storage_state, cookie_jar=jar)

    def send(body):
        return _http_fetch(args.url, args.proxy, args.insecure, hdrs, args.storage_state,
                           method=method, body=body, cookie_jar=jar)

    exchanges: list[dict] = []

    def mk(body, r):
        req_h = {"User-Agent": USER_AGENT, **hdrs}
        if body is not None:
            req_h.setdefault("Content-Type", "application/json")
        if (ck := _cookie_header(jar, args.storage_state, args.url)):
            req_h["Cookie"] = ck
        return {"method": method, "url": args.url, "req_headers": req_h, "req_body": body, "resp": r}

    report: dict = {"url": args.url, "method": method}
    base = None
    if args.body is not None:
        base = send(args.body)
        report["baseline"] = {"status": base.status, "body": _pp_body(base.body)}
        exchanges.append(mk(args.body, base))
    tamp = send(tampered)
    report["tampered"] = {"status": tamp.status, "body": _pp_body(tamp.body)}
    exchanges.append(mk(tampered, tamp))

    findings: list[Finding] = []
    if (f := probe_finding(injected, base.body if base else None, tamp.status, tamp.body, method, args.url)):
        findings.append(f)

    if args.save:
        _write_evidence(args.save, exchanges)
        report["saved"] = args.save
    sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")
    for f in findings:
        sys.stdout.write(json.dumps(f.to_dict(), ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 1 if (findings and getattr(args, "fail_on_finding", False)) else 0


def repeat_cmd(args) -> int:
    """Send one request (optionally N times) with a chosen method/body over the
    engagement session — the Repeater that recon was missing. A shared cookie jar
    keeps a server-issued session across the N sends. JSONL per send; --save writes
    the exchanges as HAR/.http evidence for a report."""
    import http.cookiejar

    hdrs = _parse_headers(args.headers)
    jar = http.cookiejar.CookieJar()
    method = args.method.upper()
    exchanges: list[dict] = []
    for i in range(args.n):
        r = _http_fetch(args.url, args.proxy, args.insecure, hdrs, args.storage_state,
                        method=method, body=args.body, cookie_jar=jar)
        if getattr(args, "save", None):
            req_h = {"User-Agent": USER_AGENT, **hdrs}
            if args.body is not None:
                req_h.setdefault("Content-Type", "application/json")
            if (ck := _cookie_header(jar, args.storage_state, args.url)):
                req_h["Cookie"] = ck
            exchanges.append({"method": method, "url": args.url, "req_headers": req_h,
                              "req_body": args.body, "resp": r})
        sys.stdout.write(json.dumps(
            {"i": i + 1, "status": r.status, "url": r.url, "body": _pp_body(r.body)},
            ensure_ascii=False) + "\n")
    if getattr(args, "save", None):
        _write_evidence(args.save, exchanges)
        print(f"zerodom: wrote {len(exchanges)} exchange(s) to {args.save}", file=sys.stderr)
    sys.stdout.flush()
    return 0


# --- Race engines -----------------------------------------------------------
# One command, one orchestration (`race_cmd`); only the send primitive swaps.
# Each engine runs setup + burst + check over ONE session via `run()`, so the
# server-issued cookie carries through. http1 is stdlib, always available, and
# proxy-native; http2 (httpx, an optional extra) multiplexes N streams on one
# connection — the browser-grade technique — and works with or without a proxy.

def _httpx_available() -> bool:
    try:
        import h2  # noqa: F401
        import httpx  # noqa: F401
        return True
    except Exception:
        return False


class Http1Sender:
    """Stdlib last-byte-sync burst. No dependency, proxy-native, instant. Can miss
    a sub-millisecond window that single-connection HTTP/2 multiplexing would hit."""
    name = "http1-lastbyte"

    def __init__(self, args, hdrs):
        import http.cookiejar
        self.a, self.hdrs = args, hdrs
        self.jar = http.cookiejar.CookieJar()

    def run(self, steps, specs, check_url):
        a = self.a
        if steps:
            _run_steps(steps, a.url, proxy=a.proxy, insecure=a.insecure,
                       headers=self.hdrs, storage_state=a.storage_state, cookie_jar=self.jar)
        statuses = _race_burst(a.url, specs, self.hdrs,
                               jar=self.jar, storage_state=a.storage_state, insecure=a.insecure)
        chk = None
        if check_url:
            chk = _pp_body(_http_fetch(check_url, a.proxy, a.insecure, self.hdrs,
                                       a.storage_state, cookie_jar=self.jar).body)
        return statuses, chk


class Http2Sender:
    """httpx HTTP/2 burst: one AsyncClient multiplexes N concurrent streams on a
    single connection (what a browser's fetch burst does). Proxy-native and works
    standalone. Reports the protocol actually negotiated, so the engine line is
    honest even if the server falls back to HTTP/1.1."""

    def __init__(self, args, hdrs):
        self.a, self.hdrs = args, hdrs
        self.name = "http2-multiplex"

    def run(self, steps, specs, check_url):
        import asyncio
        import httpx

        a = self.a
        cookies = {c["name"]: c["value"] for c in _cookies_from_state(a.storage_state)}

        def _ct(body):  # JSON content-type for a string body, matching the stdlib path
            return {"Content-Type": "application/json"} if body is not None else None

        async def _go():
            async with httpx.AsyncClient(http2=True, headers=self.hdrs, cookies=cookies,
                                         verify=not a.insecure, proxy=a.proxy, timeout=30) as client:
                for step in steps or []:
                    u = step.get("url") or urljoin(a.url, step["path"])
                    b = step.get("body")
                    if b is not None and not isinstance(b, str):
                        b = json.dumps(b)
                    await client.request(step.get("method", "GET").upper(), u, content=b, headers=_ct(b))

                async def one(method, body):
                    r = await client.request(method, a.url, content=body, headers=_ct(body))
                    return r.status_code, r.http_version

                pairs = await asyncio.gather(*[one(m, b) for m, b in specs])
                chk = None
                if check_url:
                    chk = _pp_body((await client.get(check_url)).text)
                return pairs, chk

        pairs, chk = asyncio.run(_go())
        self.name = "http2-multiplex" if any(v == "HTTP/2" for _, v in pairs) else "httpx-http1"
        return [s for s, _ in pairs], chk


def _pick_sender(args, hdrs):
    """Choose the engine: explicit --engine wins; auto uses http2 when the extra is
    installed (httpx falls back to HTTP/1.1 per-request and the report names what it
    actually used), else the stdlib http1 engine."""
    engine = getattr(args, "engine", "auto")
    if engine == "http2" and not _httpx_available():
        sys.exit("zerodom race: --engine http2 needs the optional extra — "
                 "install it with: pip install 'zerodom[race]'")
    if engine == "http1":
        return Http1Sender(args, hdrs)
    if engine == "http2":
        return Http2Sender(args, hdrs)
    return Http2Sender(args, hdrs) if _httpx_available() else Http1Sender(args, hdrs)


def race_cmd(args) -> int:
    """Fire N identical requests concurrently and report the outcome spread — a
    TOCTOU probe. More than one success on a single-use/limited action (coupon,
    claim, transfer) is the tell: the check-then-act window let duplicates through.

    Deterministic report; the human confirms intent. `--setup` reaches the pre-race
    state over a shared session; `--engine` picks the send primitive (auto prefers
    HTTP/2 multiplexing when `zerodom[race]` is installed)."""
    hdrs = _parse_headers(args.headers)
    sender = _pick_sender(args, hdrs)
    steps = json.loads(Path(args.setup).read_text(encoding="utf-8")) if args.setup else []

    # One or more --body values, each fired n times and interleaved. A mix of
    # operations on one shared record (two coupon codes on a cart) widens the
    # read-modify-write window a single repeated action can be too tight to hit.
    method = args.method.upper()
    bodies = args.body if args.body else [None]
    specs = [(method, b) for _ in range(args.n) for b in bodies]

    try:
        statuses, chk = sender.run(steps, specs, args.check)
    except Exception as exc:
        sys.exit(f"zerodom race: {sender.name} engine failed: {str(exc).splitlines()[0]}")

    report, findings = race_report(statuses, specs, method, args.url, sender.name, chk if args.check else None)
    sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")
    for f in findings:
        sys.stdout.write(json.dumps(f.to_dict(), ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 1 if (findings and getattr(args, "fail_on_finding", False)) else 0


def race_report(statuses, specs, method, url, engine, check_result):
    """Shared race outcome → (report dict, findings). The CLI and the MCP
    `zerodom_race` both feed their burst statuses (aligned to `specs`) through
    this so the JSON is identical whichever engine fired. A body that succeeded
    ≥2× over-applied a single-use action — the TOCTOU tell."""
    from collections import Counter

    from .surfaces import Finding

    dist = dict(sorted(Counter(statuses).items()))
    ok = sum(c for s, c in dist.items() if 200 <= s < 300)
    per_body: dict[str, dict] = {}
    for (_, b), st in zip(specs, statuses):
        d = per_body.setdefault(b if b is not None else "<no-body>", {"ok": 0, "dist": Counter()})
        d["dist"][st] += 1
        if 200 <= st < 300:
            d["ok"] += 1
    over = {b: d for b, d in per_body.items() if d["ok"] >= 2}
    bodies = {b for _, b in specs}

    findings: list[Finding] = []
    if over:
        which = ", ".join(f"{b} ×{d['ok']}" for b, d in over.items())
        findings.append(Finding(
            "race-condition", "high",
            f"concurrent burst to {method} {url} over-applied a single-use/limited action "
            f"({which} succeeded where one should); a check-then-act race lets duplicates through "
            f"— confirm the action is meant to be once-only",
            url))

    report: dict = {"engine": engine, "fired": len(specs), "status_distribution": dist, "succeeded": ok}
    if len(bodies) > 1:
        report["per_body"] = {b: {"succeeded": d["ok"], "status_distribution": dict(sorted(d["dist"].items()))}
                              for b, d in per_body.items()}
    if not over:
        if dist.get(429, 0) >= len(specs):
            report["note"] = ("responses were rate-limited (429), not dedup-rejected — the race is "
                              "inconclusive; retry a smaller burst or from more source IPs")
        elif engine == "http2-multiplex":
            report["note"] = ("no over-application observed over HTTP/2 multiplexing; the action may be "
                              "properly synchronized")
        else:
            report["note"] = ("no over-application observed; a very tight window may need HTTP/2 multiplexing — "
                              "retry with --engine http2 (pip install 'zerodom[race]'), and try mixing operations "
                              "with repeated --body")
    if check_result is not None:
        report["check"] = check_result
    return report, findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zerodom", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    aud = sub.add_parser(
        "audit", help="check an existing test suite's selectors against a live page"
    )
    aud.add_argument("path", help="file or directory of tests to read selectors from")
    aud.add_argument("--url", required=True, help="the running app to resolve them against")
    aud.add_argument(
        "--fail-on-ambiguous",
        action="store_true",
        help="exit non-zero if any selector matches more than one element (for CI)",
    )

    sub.add_parser(
        "relay",
        help="bridge Claude to your real, logged-in Chrome via zerodom's own extension",
    )

    ext = sub.add_parser(
        "extension", help="print the bundled Chrome extension's directory, for Load unpacked"
    )
    ext.add_argument("action", nargs="?", choices=["path"], default="path")

    sub.add_parser(
        "setup", help="guided first-run: Chromium, the extension, the MCP config and the relay"
    )

    cfg = sub.add_parser(
        "config", help="store solver settings in ~/.zerodom/config.json (env vars beat the file)"
    )
    cfg_sub = cfg.add_subparsers(dest="config_action", required=True)
    cfg_set = cfg_sub.add_parser("set", help="e.g. zerodom config set solver.provider capsolver")
    cfg_set.add_argument("key", help="solver.provider | solver.key | solver.url")
    cfg_set.add_argument("value")
    cfg_get = cfg_sub.add_parser("get", help="print one setting (keys are masked)")
    cfg_get.add_argument("key")
    cfg_sub.add_parser("list", help="print every stored setting (keys are masked)")

    scan = sub.add_parser(
        "scan", help="match a page's interaction graph against client-side surface rules"
    )
    scan.add_argument("url", help="URL (or '-' to read URLs from stdin, one per line)")
    scan.add_argument("--rules", metavar="PATH", help="YAML ruleset (default: bundled surfaces.yaml)")
    scan.add_argument("--render", action="store_true", help="fetch via headless Chromium")
    scan.add_argument("--stealth", action="store_true", help="fetch via a throwaway-profile Chrome over a CDP pipe")
    scan.add_argument("--fail-on-finding", action="store_true", help="exit non-zero if any rule fires (for CI)")
    scan.add_argument("--js", action="store_true", help="also extract leaked secrets and endpoints from inline JS")
    scan.add_argument(
        "--format", choices=["jsonl", "md"], default="jsonl", dest="fmt",
        help="jsonl (default, pipe-friendly) or md (a Markdown table to paste into a report/ticket)",
    )
    _add_fetch_flags(scan)

    crawl = sub.add_parser(
        "crawl", help="deep authenticated recon: map an app's routes/forms/API calls as JSONL"
    )
    crawl.add_argument("url", help="the start URL (crawl stays on its host by default)")
    crawl.add_argument("--scope", metavar="HOSTS", help="extra in-scope hosts, comma-separated")
    crawl.add_argument("--max-pages", type=int, default=40, dest="max_pages", help="page cap (default 40)")
    crawl.add_argument("--deny", default=_DEFAULT_DENY, help="regex of links to never follow (logout/delete/…)")
    crawl.add_argument(
        "--relay", action="store_true",
        help="crawl through your real, logged-in Chrome via the ZeroDOM relay — an "
             "already-authenticated session instead of a fresh headless browser",
    )
    crawl.add_argument(
        "--stealth", action="store_true",
        help="(crawl has no stealth transport — kept only so the --relay clash reports "
             "clearly; see the error text)",
    )
    _add_fetch_flags(crawl)

    cmp = sub.add_parser(
        "compare", help="fetch a URL under two saved sessions and diff the responses (cross-tenant IDOR)"
    )
    cmp.add_argument("url", help="the URL (or '-' to read URLs from stdin, one per line)")
    cmp.add_argument(
        "--as", metavar="NAME=STATE.json", action="append", dest="identities", required=True,
        help="a named identity and its storage_state file, repeatable — pass at least two",
    )
    _add_fetch_flags(cmp)

    swp = sub.add_parser(
        "sweep",
        help="crawl as one identity, then replay every in-scope URL across identities to flag broken access control",
    )
    swp.add_argument("url", help="the start URL (crawl stays on its host by default)")
    swp.add_argument(
        "--as", metavar="NAME=STATE.json", action="append", dest="identities", required=True,
        help="a named identity and its storage_state file, repeatable — pass at least two",
    )
    swp.add_argument("--scope", metavar="HOSTS", help="extra in-scope hosts, comma-separated")
    swp.add_argument("--max-pages", type=int, default=40, dest="max_pages", help="crawl page cap (default 40)")
    swp.add_argument("--deny", default=_DEFAULT_DENY, help="regex of URLs to never follow or replay (logout/delete/…)")
    swp.add_argument("--rps", type=float, default=None,
                     help="cap replay requests per second to stay under a program's rate limit (default: unthrottled)")
    swp.add_argument(
        "--format", choices=["jsonl", "md"], default="jsonl", dest="fmt",
        help="jsonl (default) or md (a Markdown table for a report/ticket)",
    )
    _add_fetch_flags(swp)  # --storage-state here overrides the crawl identity (defaults to the first --as)

    rep = sub.add_parser(
        "repeat", help="send a request (optionally N times) with a chosen method/body — a Repeater over the session",
    )
    rep.add_argument("url", help="the target URL")
    rep.add_argument("--method", default="GET", help="HTTP method (default GET)")
    rep.add_argument("--body", help="request body (a JSON string sets Content-Type: application/json)")
    rep.add_argument("-n", type=int, default=1, dest="n", help="send it this many times (default 1)")
    rep.add_argument("--save", metavar="FILE",
                     help="write the request/response exchanges as evidence: .http (readable) "
                          "or .har (default; imports into Burp/Caido/browser)")
    _add_fetch_flags(rep)

    prb = sub.add_parser(
        "probe", help="replay an endpoint with a tampered body and diff vs baseline (mass assignment / tampering)",
    )
    prb.add_argument("url", help="the target endpoint")
    prb.add_argument("--method", default="POST", help="HTTP method (default POST)")
    prb.add_argument("--body", help="baseline request body (JSON)")
    prb.add_argument("--tamper", metavar="JSON", required=True,
                     help="fields to inject, merged over --body, e.g. '{\"role\":\"admin\",\"price\":1}'")
    prb.add_argument("--setup", metavar="STEPS.json",
                     help="JSON array of prep requests [{url|path, method?, body?}] run before the probe")
    prb.add_argument("--save", metavar="FILE", help="write baseline+tampered exchanges as .har/.http evidence")
    prb.add_argument("--fail-on-finding", action="store_true", dest="fail_on_finding",
                     help="exit non-zero if a finding is flagged (for CI)")
    _add_fetch_flags(prb)

    rc = sub.add_parser(
        "race", help="fire N identical requests concurrently and flag a check-then-act race (TOCTOU)",
    )
    rc.add_argument("url", help="the target URL to race")
    rc.add_argument("--method", default="POST", help="HTTP method (default POST)")
    rc.add_argument("--body", action="append",
                    help="request body (JSON string → Content-Type: application/json). Repeatable: "
                         "each body is fired n times and interleaved, to race a mix of operations on one record")
    rc.add_argument("-n", type=int, default=20, dest="n", help="copies per body fired concurrently (default 20)")
    rc.add_argument("--setup", metavar="STEPS.json",
                    help="JSON array of prep requests [{url|path, method?, body?}] run before the burst")
    rc.add_argument("--check", metavar="URL", help="GET this after the burst and include its state in the report")
    rc.add_argument("--engine", choices=["auto", "http1", "http2"], default="auto",
                    help="send engine: http1 (stdlib last-byte-sync, always available) or http2 "
                         "(httpx multiplexing, needs 'pip install zerodom[race]'); auto prefers http2 when installed")
    rc.add_argument("--fail-on-finding", action="store_true", dest="fail_on_finding",
                    help="exit non-zero if a race is flagged (for CI)")
    _add_fetch_flags(rc)

    insp = sub.add_parser("inspect", help="parse a URL and report token savings")
    insp.add_argument("url")
    insp.add_argument(
        "--render",
        action="store_true",
        help="fetch via headless Chromium instead of plain HTTP (for JS-rendered pages)",
    )
    insp.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit the full JSON graph instead of the token-dense text DSL",
    )
    insp.add_argument(
        "--frames",
        action="store_true",
        help="also read inside iframes (implies --render; embedded editors, payment fields)",
    )
    insp.add_argument(
        "--find",
        metavar="QUERY",
        help="print only the nodes whose type or label matches QUERY",
    )
    insp.add_argument(
        "--stealth",
        action="store_true",
        help="fetch via a throwaway-profile Chrome over a CDP pipe (no port, no Playwright)",
    )
    insp.add_argument(
        "--pipe",
        dest="as_pipe",
        action="store_true",
        help="emit the node array as JSONL to stdout (one node per line) instead of the report",
    )
    insp.add_argument(
        "--screenshot",
        metavar="OUT.png",
        help="write a full-page screenshot with a numbered badge over every node",
    )
    insp.add_argument(
        "--html",
        dest="html_path",
        metavar="OUT.html",
        help="write a self-contained HTML report: graph beside the annotated page",
    )
    _add_fetch_flags(insp)

    # `inspect` is the common verb, so requiring it is ceremony: `zerodom <url>`
    # works, and the explicit form keeps working for anyone who learned it.
    argv = sys.argv[1:] if argv is None else argv
    if (
        argv
        and argv[0] not in {"inspect", "audit", "relay", "scan", "compare", "sweep", "crawl", "repeat", "probe", "race", "extension", "setup", "config", "-h", "--help"}
        and not argv[0].startswith("-")
    ):
        argv = ["inspect", *argv]

    args = parser.parse_args(argv)
    # --no-solver reaches every fetch verb through _add_fetch_flags; set it as
    # the process-wide default before any navigation builds a Policy.
    clearance.set_defaults(no_solver=bool(getattr(args, "no_solver", False)))
    if args.command == "config":
        return config_cmd(args)
    if args.command == "audit":
        from .audit import run

        ambiguous, text = run(args.path, args.url)
        print(text)
        return 1 if (ambiguous and args.fail_on_ambiguous) else 0

    if args.command == "relay":
        from .relay import main as relay_main

        relay_main()
        return 0

    if args.command == "scan":
        return scan_targets(args)

    if args.command == "repeat":
        return repeat_cmd(args)

    if args.command == "probe":
        return probe_cmd(args)

    if args.command == "race":
        return race_cmd(args)

    if args.command == "crawl":
        # Same shape as inspect's --frames+--stealth guard: two browser
        # transports that can't compose say so, instead of one winning quietly.
        if args.relay and args.stealth:
            raise SystemExit("zerodom: --relay and --stealth can't be combined "
                             "(--relay drives your real Chrome via the relay; --stealth is the CDP pipe)")
        if args.stealth:
            # crawl needs page-level request events to record API calls, and the
            # CDP-pipe transport is one-shot (fetch only, cdp_pipe.py) — so this
            # combination has no implementation, rather than a worse one.
            raise SystemExit(
                "zerodom crawl: --stealth isn't available on the crawl path — the CDP-pipe "
                "transport is one-shot (fetch only) and crawl needs page-level request events "
                "to record API calls. Use --relay (real authenticated session), or "
                "`zerodom inspect --stealth <url>` for a single page.")
        scope = set(h.strip() for h in (args.scope or "").split(",") if h.strip())
        for record in crawl_site(
            args.url, scope=scope, max_pages=args.max_pages, deny=args.deny,
            proxy=args.proxy, insecure=args.insecure,
            headers=_parse_headers(args.headers), storage_state=args.storage_state,
            relay=args.relay,
        ):
            sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        return 0

    if args.command == "compare":
        ids = _parse_identities(args.identities)
        hdrs = _parse_headers(args.headers)
        urls = _stdin_urls() if args.url == "-" else [args.url]
        for url in urls:
            try:
                result = compare_identities(url, ids, proxy=args.proxy,
                                            insecure=args.insecure, headers=hdrs)
            except (urllib.error.URLError, ValueError, OSError) as exc:
                # Sweeping ids over a range (seq | compare -) can't die on one dead URL.
                print(f"zerodom: could not fetch {url}: {getattr(exc, 'reason', exc)}",
                      file=sys.stderr)
                continue
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0

    if args.command == "sweep":
        ids = _parse_identities(args.identities)
        scope = set(h.strip() for h in (args.scope or "").split(",") if h.strip())
        findings = sweep_identities(
            args.url, ids, crawl_state=args.storage_state, scope=scope or None,
            max_pages=args.max_pages, deny=args.deny, proxy=args.proxy,
            insecure=args.insecure, headers=_parse_headers(args.headers), rps=args.rps,
        )
        if args.fmt == "md":
            sys.stdout.write(_findings_markdown(findings))
        else:
            for f in findings:
                sys.stdout.write(json.dumps(f.to_dict(), ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0

    if args.command == "setup":
        return setup(args)

    if args.command == "extension":
        path = extension_dir()
        # Path alone on stdout so `cd "$(zerodom extension)"` works; the how-to goes to stderr.
        print(path)
        print("chrome://extensions -> Developer mode -> Load unpacked -> select the directory above",
              file=sys.stderr)
        return 0

    headers = _parse_headers(args.headers)
    # `-` means: read URLs from stdin, one per line (cat targets.txt | zerodom inspect -).
    if args.url == "-":
        rc = 0
        for url in _stdin_urls():
            rc |= inspect(
                url, args.render, args.as_json, args.screenshot,
                args.html_path, args.find, args.frames, args.stealth, args.as_pipe,
                args.proxy, args.insecure, headers, args.storage_state,
            )
        return rc

    return inspect(
        args.url, args.render, args.as_json, args.screenshot,
        args.html_path, args.find, args.frames, args.stealth, args.as_pipe,
        args.proxy, args.insecure, headers, args.storage_state,
    )


if __name__ == "__main__":
    sys.exit(main())
