"""JS intelligence for `zerodom scan --js`: deterministic regex over a page's
scripts and markup for leaked secrets, interesting endpoints, the API call shape
(method + body keys), and DOM-XSS sinks.

The CLAUDE.md template every bug hunter keeps logs "endpoints/feature flags
found in the JS" and stray keys by hand. This is that pass, mechanised: no LLM,
no expression language, just tight patterns and `re.finditer`. It complements
`surfaces.py` — that judges the interaction graph the parser built, this reads
the raw source the parser threw away.

Scope: inline `<script>` blocks, the whole HTML (a key is a key wherever it
sits), and — when the caller passes them — the fetched bodies of linked/bundled
scripts. This module stays network-free by design: the caller (cli) owns fetching
and hands the external script texts in via `scripts=`, so the analysis is a pure
function of its inputs and the `_selfcheck` needs no server. Findings reuse
`surfaces.Finding` so they stream through the same JSONL path.
"""

from __future__ import annotations

import re

from lxml import html as lxml_html

from .surfaces import Finding

# High-signal secrets: a hit is worth a human look, and each pattern is anchored
# tightly enough that random text won't trip it. The matched value is redacted
# in the finding so the scan log isn't itself a fresh copy of the leak.
_SECRETS: list[tuple[str, str, re.Pattern[str]]] = [
    ("aws-access-key", "high", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google-api-key", "high", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe-live-key", "high", re.compile(r"\bsk_live_[0-9A-Za-z]{24,}\b")),
    ("slack-token", "high", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("github-token", "high", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b")),
    ("private-key", "high", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("jwt", "medium", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b")),
]

# Endpoint literals: paths under surfaces an agent should probe (api/admin/
# internal/graphql/versioned), whether written bare or in a fetch/axios/xhr call.
_ENDPOINTS = re.compile(
    r"""["'`](/(?:api|graphql|internal|admin|debug|v\d+)/[A-Za-z0-9_./:{}\-]*)["'`]"""
)

# Call sites: fetch(path, {...}), axios.post(path, {...}), $.post(path, ...), and
# any wrapper — api(path, {...}), request(path, …) — since apps route fetch through
# a helper. A generic identifier is the last alternative so the named verbs win;
# the path must be a "/"-literal, which filters out non-HTTP calls.
# ponytail: regex body-shape; swap for tree-sitter-js only if minified bundles
# defeat the bounded-tail read.
_CALL = re.compile(
    r"""(?:fetch|axios(?:\.(?P<verb>get|post|put|patch|delete|head))?|\$\.(?:ajax|get|post)"""
    r"""|[A-Za-z_$][\w$]*)"""
    r"""\s*\(\s*["'`](?P<path>/[^"'`]+)["'`](?P<tail>[^;]{0,400})""",
    re.IGNORECASE | re.DOTALL,
)
_METHOD = re.compile(r"""method\s*:\s*["'`](?P<m>[A-Za-z]+)["'`]""", re.IGNORECASE)
# Body object: the {...} inside JSON.stringify(...) or a `body:`/`data:` literal.
_BODY = re.compile(r"""(?:JSON\.stringify|body|data)\s*:?\s*\(?\s*\{(?P<obj>[^{}]*)\}""", re.IGNORECASE)
# Leading identifier of one comma-separated member — handles `code` (shorthand),
# `code: x` and `"code": x` alike.
_KEY = re.compile(r"""^\s*['"]?(?P<k>[A-Za-z_$][\w$]*)""")

# DOM-XSS sinks. Two patterns, because the dangerous string is often built in a
# helper that *returns* markup and is assigned to innerHTML elsewhere:
#   A) any backtick template literal that builds HTML (contains a `<tag`) and has
#      an interpolation not wrapped by an escaper — catches `productCard()` et al.
#   B) a direct write to a sink keyword whose RHS is a template with an unescaped
#      interpolation — catches inline `el.innerHTML = `${x}``, even without a tag.
_TEMPLATE = re.compile(r"`(?P<body>(?:[^`\\]|\\.)*)`", re.DOTALL)
_HTML_TAG = re.compile(r"<[A-Za-z/!]")
_SINK = re.compile(
    r"""(?P<sink>innerHTML|outerHTML|insertAdjacentHTML|document\.write(?:ln)?|"""
    r"""\beval|new\s+Function)\s*(?:=|\()\s*(?P<rhs>`(?:[^`\\]|\\.)*`)""",
    re.IGNORECASE | re.DOTALL,
)
_ESCAPERS = re.compile(r"escapeHtml|encodeURI(?:Component)?|DOMPurify|sanitize|textContent", re.IGNORECASE)
_INTERP = re.compile(r"\$\{(?P<expr>[^}]*)\}")

# Attacker-controllable DOM sources. A sink fed (directly or through a variable) by
# one of these is reachable XSS; a sink fed by server/numeric data is not, so it is
# reported low instead of high — real, but not exploitable from a crafted link.
_SOURCE = re.compile(
    r"location\b|\.hash\b|\.search\b|\blocation\.href\b|URLSearchParams|"
    r"postMessage|\bevent\.data\b|document\.referrer|window\.name\b|document\.cookie|"
    r"localStorage|sessionStorage|\.getItem\b",
    re.IGNORECASE)
_ASSIGN = re.compile(r"(?:var|let|const)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?P<rhs>[^;\n]+)")
_ROOT_ID = re.compile(r"[A-Za-z_$][\w$]*")


def _tainted_vars(js_text: str) -> set[str]:
    """Variable names assigned (transitively, one hop) from a DOM source — enough
    to tell `let q = location.hash` apart from `let total = subtotal`."""
    tainted: set[str] = set()
    for _ in range(2):  # one extra pass catches `b = a` where a is already tainted
        for m in _ASSIGN.finditer(js_text):
            rhs = m.group("rhs")
            if _SOURCE.search(rhs) or any(t in _ROOT_ID.findall(rhs) for t in tainted):
                tainted.add(m.group("name"))
    return tainted


def _redact(s: str) -> str:
    """Keep enough of a secret to recognise it, not enough to reuse it."""
    if len(s) <= 12:
        return s[:3] + "…"
    return f"{s[:6]}…{s[-4:]}"


def _script_text(doc) -> str:
    return "\n".join(s.text_content() for s in doc.iter("script") if s.text_content())


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def extract_endpoints(js_text: str, source: str = "") -> list[dict]:
    """API call sites in JS: method + path + body keys, one dict per call.

    `source` labels where it came from (e.g. the script URL). Deduped on
    (method, path) in stable order so runs over the same bundle are identical."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in _CALL.finditer(js_text):
        path = m.group("path")
        tail = m.group("tail") or ""
        verb = m.group("verb")
        mt = _METHOD.search(tail)
        method = (verb or (mt.group("m") if mt else "GET")).upper()
        body = _BODY.search(tail)
        keys: list[str] = []
        if body:
            seen_k: set[str] = set()
            for part in body.group("obj").split(","):
                km = _KEY.match(part)
                if km and km.group("k") not in seen_k:
                    seen_k.add(km.group("k"))
                    keys.append(km.group("k"))
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        out.append({"method": method, "path": path, "body_keys": keys, "source": source})
    return sorted(out, key=lambda e: (e["path"], e["method"]))


def scan_sinks(js_text: str, base_url: str, source: str = "") -> list[Finding]:
    """DOM-XSS sinks: an HTML-building template literal (or a direct sink write)
    whose interpolation is not wrapped by an escaper.

    Proximity heuristic, not real taint/data-flow — it flags the line for a human
    to confirm. Each `${…}` group is inspected: a group that passes through an
    escaper is safe; one that doesn't makes the write high. A template with no
    `<tag` and no sink keyword carries no HTML risk, so it's left alone (that's
    how `Hello ${name}` stays quiet while `<a class="x-${v}">` is flagged)."""
    findings: list[Finding] = []
    seen: set[int] = set()
    tainted = _tainted_vars(js_text)

    def _reach(expr: str) -> bool:
        """Is this interpolation reachable from an attacker source — directly, or
        through a variable assigned from one?"""
        if _SOURCE.search(expr):
            return True
        return any(ident in tainted for ident in _ROOT_ID.findall(expr))

    def _report(pos: int, kind: str, unescaped: list[str]) -> None:
        line = _line(js_text, pos)
        if line in seen:
            return
        seen.add(line)
        sample = unescaped[0].strip()[:60]
        where = f"{source}:{line}" if source else f"line {line}"
        reachable = any(_reach(e) for e in unescaped)
        sev = "high" if reachable else "low"
        tag = ("reachable from an attacker-controllable source"
               if reachable else "no attacker source traced — likely server/trusted data")
        findings.append(Finding(
            rule="dom-sink", severity=sev,
            description=f"unescaped interpolation in {kind} at {where}: ${{{sample}}} ({tag})",
            url=base_url,
        ))

    # B) direct sink write with a template RHS (reported first so it wins the line).
    for m in _SINK.finditer(js_text):
        unescaped = [e for e in _INTERP.findall(m.group("rhs")) if not _ESCAPERS.search(e)]
        if unescaped:
            _report(m.start(), m.group("sink"), unescaped)

    # A) any HTML-building template literal with an unescaped interpolation.
    for m in _TEMPLATE.finditer(js_text):
        body = m.group("body")
        if not _HTML_TAG.search(body):
            continue
        unescaped = [e for e in _INTERP.findall(body) if not _ESCAPERS.search(e)]
        if unescaped:
            _report(m.start(), "HTML template literal", unescaped)

    return findings


def scan_js(html: str, base_url: str, scripts: list[str] | None = None) -> list[Finding]:
    """Leaked secrets, endpoints (bare + call-shaped) and DOM sinks found in a
    page's source. `scripts` are the fetched bodies of linked/bundled JS the
    caller pulled in (this module never fetches). Deduped and in a stable order
    (secrets, endpoints, sinks)."""
    try:
        doc = lxml_html.fromstring(html)
        inline = _script_text(doc)
    except Exception:
        inline = ""  # malformed markup: still scan the raw text below

    ext = scripts or []
    # All JS the analysis can see: inline first, then each external body, labelled.
    labelled: list[tuple[str, str]] = [("inline", inline)]
    labelled += [(f"script[{i}]", s) for i, s in enumerate(ext)]
    all_js = "\n".join(s for _, s in labelled)

    findings: list[Finding] = []

    seen_secrets: set[tuple[str, str]] = set()
    for name, severity, pat in _SECRETS:
        for m in pat.finditer(html + "\n" + all_js):
            val = m.group(0)
            key = (name, val)
            if key in seen_secrets:
                continue
            seen_secrets.add(key)
            findings.append(Finding(
                rule=f"js-secret:{name}", severity=severity,
                description=f"{name} in page source: {_redact(val)}", url=base_url,
            ))

    # Call-shaped endpoints (method + body keys) across inline + external JS.
    call_eps: list[dict] = []
    seen_call: set[tuple[str, str]] = set()
    for label, js in labelled:
        for ep in extract_endpoints(js, source=label):
            k = (ep["method"], ep["path"])
            if k in seen_call:
                continue
            seen_call.add(k)
            call_eps.append(ep)
    endpoints: list[Finding] = []
    for ep in call_eps:
        body = f" body={{{','.join(ep['body_keys'])}}}" if ep["body_keys"] else ""
        endpoints.append(Finding(
            rule="js-endpoint", severity="low",
            description=f"{ep['method']} {ep['path']}{body} (called in JS)", url=base_url,
        ))
    # Bare path literals not already seen as a call site (keeps old behaviour).
    seen_ep: set[str] = {ep["path"] for ep in call_eps}
    for m in _ENDPOINTS.finditer(all_js or html):
        path = m.group(1)
        if path in seen_ep:
            continue
        seen_ep.add(path)
        endpoints.append(Finding(
            rule="js-endpoint", severity="low",
            description=f"endpoint referenced in JS: {path}", url=base_url,
        ))

    # DOM sinks across inline + external JS.
    sinks: list[Finding] = []
    for label, js in labelled:
        sinks += scan_sinks(js, base_url, source=label)

    secrets = [f for f in findings if f.rule.startswith("js-secret")]
    endpoints = sorted(endpoints, key=lambda f: f.description)
    return secrets + endpoints + sinks


def _selfcheck() -> None:
    html = """
    <html><body>
      <a href="/api/public">visible link, not from JS</a>
      <script>
        const AWS = "AKIAIOSFODNN7EXAMPLE";
        fetch("/api/v1/admin/refund", {method:"POST"});
        const t = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abcdef123456";
        axios.get("/internal/flags");
      </script>
    </body></html>
    """
    out = scan_js(html, "https://t.example")
    rules = [f.rule for f in out]
    assert "js-secret:aws-access-key" in rules, rules
    assert "js-secret:jwt" in rules, rules
    # Inline call sites are parsed for method; the bare axios.get path still shows.
    descs = " | ".join(f.description for f in out if f.rule == "js-endpoint")
    assert "POST /api/v1/admin/refund" in descs, descs
    assert "/internal/flags" in descs, descs
    assert "/api/public" not in descs, descs  # visible href, not in a <script>
    assert all("AKIAIOSFODNN7EXAMPLE" not in f.description for f in out)

    # External bundle (the app.js case): method + body shape + a DOM sink.
    app_js = r"""
        const escapeHtml = v => String(v).replace(/[&<>'"]/g, c => c);
        async function addToCart(productId){
          await fetch("/api/cart/items", {method:"POST", body: JSON.stringify({ productId })});
        }
        async function applyCoupon(code){
          await fetch("/api/cart/coupon", {method:"POST", body: JSON.stringify({ code })});
        }
        function productCard(p){
          return `<a class="image-${p.image}" href="/x">${escapeHtml(p.name)}</a>`;
        }
    """
    out2 = scan_js("<html><body></body></html>", "https://shop.example", scripts=[app_js])
    eps = {(d.split()[0], d.split()[1]) for d in (f.description for f in out2 if f.rule == "js-endpoint") if d[0].isupper()}
    assert ("POST", "/api/cart/coupon") in eps, eps
    assert ("POST", "/api/cart/items") in eps, eps
    body_descs = " | ".join(f.description for f in out2 if f.rule == "js-endpoint")
    assert "body={code}" in body_descs, body_descs
    assert "body={productId}" in body_descs, body_descs
    sinks = [f for f in out2 if f.rule == "dom-sink"]
    # image-${p.image} is unescaped but fed by server data → reported low, not high.
    assert len(sinks) == 1 and sinks[0].severity == "low", [s.description for s in sinks]
    assert "p.image" in sinks[0].description, sinks[0].description

    # Reachability: a sink fed from location.hash (directly or via a var) is high;
    # the escaped one stays quiet.
    out3 = scan_js("<html></html>", "https://x.example", scripts=[
        'let q = location.hash.slice(1);\n'
        'box.innerHTML = `<div>${q}</div>`;\n'
        'ok.innerHTML = `code: ${escapeHtml(c)}`;'])
    s3 = [f for f in out3 if f.rule == "dom-sink"]
    assert len(s3) == 1 and s3[0].severity == "high", [s.description for s in s3]
    assert "attacker-controllable" in s3[0].description, s3[0].description
    print("jsintel selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
