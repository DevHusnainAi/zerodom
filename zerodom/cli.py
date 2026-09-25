"""Turn a web page into the list of things an agent can click, and count the
savings. `zerodom <url>` for the compact graph, --find to search it."""

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

import tiktoken

from .parser import ZeroDOMParser, compact_line, find_nodes
from .playwright_wrapper import serialize

USER_AGENT = "Mozilla/5.0 (compatible; zerodom)"


def count_tokens(text: str) -> int:
    return len(tiktoken.get_encoding("cl100k_base").encode(text))


def _goto(page, url: str) -> None:
    """Navigate without hanging on a page that never goes network-idle.

    `wait_until="networkidle"` hangs the full 30s on any Turnstile/CF challenge
    or long-poll page (the widget keeps a socket open forever). Commit on DOM
    ready, then give the network a short bounded window to settle for SPAs and
    read whatever is there if it doesn't."""
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass  # never idles (challenge widget / long-poll) — read the page as-is


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

    _status, html, final = _http_fetch(url, proxy, insecure, headers, storage_state)
    return html, final


def _http_fetch(url, proxy, insecure, headers, storage_state) -> tuple[int, str, str]:
    """Plain HTTP with the engagement's proxy/headers/cookies, returning the
    status too — `compare` needs 403-vs-200, which the graph alone can't show."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for c in _cookies_from_state(storage_state):
        req.add_header("Cookie", f"{c['name']}={c['value']}")
    hs: list = []
    if proxy:
        hs.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    if insecure:
        import ssl
        hs.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    opener = urllib.request.build_opener(*hs)
    try:
        with opener.open(req, timeout=30) as resp:
            body = resp.read().decode(resp.headers.get_content_charset() or "utf-8", "replace")
            return resp.status, body, resp.url
    except urllib.error.HTTPError as e:  # 401/403/404 are the whole point of an IDOR check
        return e.code, e.read().decode("utf-8", "replace"), url


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
):
    """Deep, authenticated, read-only recon of a real app: BFS-walk same-scope
    routes in a rendered browser (so JS SPAs actually load), and for each page
    record the forms, links, and the API calls it fired. This is the attack-
    surface map a hunter builds by hand — routes, params, endpoints — yielded one
    JSON object per page.

    Read-only by design: it navigates and reads, never submits a form or clicks a
    control matching `deny` (logout/delete/…), so it's safe to run unattended.
    """
    import re as _re
    from urllib.parse import urljoin, urlparse

    from lxml import html as lxml_html
    from playwright.sync_api import sync_playwright

    deny_re = _re.compile(deny, _re.I)
    start_host = urlparse(start_url).netloc
    allow = {start_host} | (scope or set())

    def in_scope(u: str) -> bool:
        h = urlparse(u).netloc
        return h in allow and not deny_re.search(u)

    def forms_of(doc, base: str) -> list[dict]:
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

    seen: set[str] = set()
    queue: list[str] = [start_url]
    with sync_playwright() as pw:
        browser = _launch(pw, proxy)
        ctx = _context(browser, insecure=insecure, headers=headers, storage_state=storage_state)
        page = ctx.new_page()
        api_calls: list[str] = []
        # XHR/fetch the page fires during a load = the app's real API surface.
        page.on("request", lambda r: api_calls.append(f"{r.method} {r.url}")
                if r.resource_type in ("xhr", "fetch") else None)
        while queue and len(seen) < max_pages:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            api_calls.clear()
            try:
                _goto(page, url)
                html, final = serialize(page), page.url
            except Exception as exc:
                yield {"url": url, "error": str(exc)[:200]}
                continue
            graph = ZeroDOMParser(html, final).parse()
            try:
                doc = lxml_html.fromstring(html)
            except Exception:
                doc = None
            links = sorted({
                urljoin(final, n["href"]) for n in graph["nodes"]
                if n.get("href") and n["href"].split(":", 1)[0] not in ("javascript", "mailto", "tel")
            })
            forms = forms_of(doc, final) if doc is not None else []
            record = {
                "url": final,
                "forms": forms,
                "api_calls": sorted(set(api_calls)),
                "hidden_fields": [h["name"] for h in graph["metadata"].get("hidden_fields", [])],
                "links_in_scope": [l for l in links if in_scope(l)],
            }
            if blocked := graph["metadata"].get("blocked"):
                record["blocked"] = blocked["kind"]
            yield record
            for l in record["links_in_scope"]:
                if l not in seen and l not in queue:
                    queue.append(l)
        browser.close()


def compare_identities(
    url: str,
    identities: list[tuple[str, str]],
    *,
    proxy: str | None = None,
    insecure: bool = False,
    headers: dict | None = None,
) -> dict:
    """Fetch one URL under each identity (a name + its storage_state cookies) and
    diff the responses — the cross-tenant IDOR primitive: does user A see user B's
    data? Byte-identical bodies across two identities on a per-user resource is
    the tell. Deterministic; interpretation stays with the human."""
    per: dict[str, dict] = {}
    bodies: dict[str, str] = {}
    for name, state in identities:
        status, body, final = _http_fetch(url, proxy, insecure, headers, state)
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


def emit_jsonl(graph) -> None:
    """The flattened 1D node array as clean JSONL: one page header line, then one
    node per line. Greppable, streamable, zero pretty-printing overhead."""
    meta = graph["metadata"]
    out = sys.stdout
    out.write(json.dumps({"url": meta["url"], "nodes": len(graph["nodes"])}) + "\n")
    for node in graph["nodes"]:
        out.write(json.dumps(node, ensure_ascii=False) + "\n")
    out.flush()


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
        print(f"  Blocked            {blocked['kind']} challenge detected "
              f"(reuse a cleared session with --storage-state, or drive it in relay mode)",
              file=report)
    if extra:
        print(extra, file=report)
    print("─" * 52, file=report)
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
    from .surfaces import evaluate, load_rules

    try:
        rules = load_rules(args.rules)  # loads once; raises loudly on a bad ruleset
    except Exception as exc:
        # A --rules file is config: a missing file, bad YAML, or an invalid rule
        # (load_rules' own ValueErrors are already user-readable) is a clean exit,
        # not a traceback dumped at someone writing their first ruleset.
        sys.exit(f"zerodom: bad rules file: {exc}")
    urls = _stdin_urls() if args.url == "-" else [args.url]
    any_finding = False
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
        if args.js:
            from .jsintel import scan_js
            findings += scan_js(html, final_url)
        for finding in findings:
            any_finding = True
            sys.stdout.write(json.dumps(finding.to_dict(), ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 1 if (any_finding and args.fail_on_finding) else 0


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


def _parse_headers(pairs: list | None) -> dict:
    """`--header 'K: V'` values → {K: V}. A header without a colon is a user error."""
    out = {}
    for raw in pairs or []:
        if ":" not in raw:
            raise SystemExit(f"zerodom: --header needs 'Key: Value', got {raw!r}")
        k, v = raw.split(":", 1)
        out[k.strip()] = v.strip()
    return out


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
        raise SystemExit("zerodom: compare needs at least two --as identities to diff")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zerodom", description=__doc__)
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

    scan = sub.add_parser(
        "scan", help="match a page's interaction graph against client-side surface rules"
    )
    scan.add_argument("url", help="URL (or '-' to read URLs from stdin, one per line)")
    scan.add_argument("--rules", metavar="PATH", help="YAML ruleset (default: bundled surfaces.yaml)")
    scan.add_argument("--render", action="store_true", help="fetch via headless Chromium")
    scan.add_argument("--stealth", action="store_true", help="fetch via a throwaway-profile Chrome over a CDP pipe")
    scan.add_argument("--fail-on-finding", action="store_true", help="exit non-zero if any rule fires (for CI)")
    scan.add_argument("--js", action="store_true", help="also extract leaked secrets and endpoints from inline JS")
    _add_fetch_flags(scan)

    crawl = sub.add_parser(
        "crawl", help="deep authenticated recon: map an app's routes/forms/API calls as JSONL"
    )
    crawl.add_argument("url", help="the start URL (crawl stays on its host by default)")
    crawl.add_argument("--scope", metavar="HOSTS", help="extra in-scope hosts, comma-separated")
    crawl.add_argument("--max-pages", type=int, default=40, dest="max_pages", help="page cap (default 40)")
    crawl.add_argument("--deny", default=_DEFAULT_DENY, help="regex of links to never follow (logout/delete/…)")
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
        and argv[0] not in {"inspect", "audit", "relay", "scan", "compare", "crawl", "extension", "setup", "-h", "--help"}
        and not argv[0].startswith("-")
    ):
        argv = ["inspect", *argv]

    args = parser.parse_args(argv)
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

    if args.command == "crawl":
        scope = set(h.strip() for h in (args.scope or "").split(",") if h.strip())
        for record in crawl_site(
            args.url, scope=scope, max_pages=args.max_pages, deny=args.deny,
            proxy=args.proxy, insecure=args.insecure,
            headers=_parse_headers(args.headers), storage_state=args.storage_state,
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
