"""JS intelligence for `zerodom scan --js`: deterministic regex over a page's
inline scripts and markup for leaked secrets and interesting endpoints.

The CLAUDE.md template every bug hunter keeps logs "endpoints/feature flags
found in the JS" and stray keys by hand. This is that pass, mechanised: no LLM,
no expression language, just tight patterns and `re.finditer`. It complements
`surfaces.py` — that judges the interaction graph the parser built, this reads
the raw source the parser threw away.

Scope: inline `<script>` blocks plus the whole HTML (a key is a key wherever it
sits — a data-attribute, a JSON blob). Linked/bundled JS is not fetched here;
that is a per-script network fan-out and its own feature. Findings reuse
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


def _redact(s: str) -> str:
    """Keep enough of a secret to recognise it, not enough to reuse it."""
    if len(s) <= 12:
        return s[:3] + "…"
    return f"{s[:6]}…{s[-4:]}"


def _script_text(doc) -> str:
    return "\n".join(s.text_content() for s in doc.iter("script") if s.text_content())


def scan_js(html: str, base_url: str) -> list[Finding]:
    """Leaked secrets and interesting endpoints found in a page's source, deduped
    and in a stable order (secrets first, then endpoints, each sorted)."""
    try:
        doc = lxml_html.fromstring(html)
        scripts = _script_text(doc)
    except Exception:
        scripts = ""  # malformed markup: still scan the raw text below

    findings: list[Finding] = []

    seen_secrets: set[tuple[str, str]] = set()
    for name, severity, pat in _SECRETS:
        for m in pat.finditer(html):
            val = m.group(0)
            key = (name, val)
            if key in seen_secrets:
                continue
            seen_secrets.add(key)
            findings.append(Finding(
                rule=f"js-secret:{name}", severity=severity,
                description=f"{name} in page source: {_redact(val)}", url=base_url,
            ))

    # Endpoints from inline scripts (path literals in bundled JS are the target;
    # scanning script text, not the whole HTML, keeps hrefs/visible text out).
    seen_ep: set[str] = set()
    for m in _ENDPOINTS.finditer(scripts or html):
        path = m.group(1)
        if path in seen_ep:
            continue
        seen_ep.add(path)
        findings.append(Finding(
            rule="js-endpoint", severity="low",
            description=f"endpoint referenced in JS: {path}", url=base_url,
        ))

    # Secrets sorted by their (already fixed) discovery, endpoints sorted by path
    # so runs over the same page are byte-identical.
    secrets = [f for f in findings if f.rule.startswith("js-secret")]
    endpoints = sorted((f for f in findings if f.rule == "js-endpoint"), key=lambda f: f.description)
    return secrets + endpoints


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
    eps = sorted(f.description.split(": ")[1] for f in out if f.rule == "js-endpoint")
    assert eps == ["/api/v1/admin/refund", "/internal/flags"], eps
    # The visible <a href="/api/public"> is not inside a <script>, so it's not an endpoint finding.
    assert "/api/public" not in eps, eps
    # Redaction: the raw AWS key never appears verbatim in a finding.
    assert all("AKIAIOSFODNN7EXAMPLE" not in f.description for f in out)
    print("jsintel selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
