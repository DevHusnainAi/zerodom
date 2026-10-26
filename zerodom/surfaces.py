"""Deterministic client-side surface rules over a parsed InteractionGraph.

`audit.py` checks selectors a human already wrote; this checks the *page* — the
clickable/fillable surface the parser found — against YAML rules, for the
appsec smells an agent or operator wants flagged: inputs named like secrets,
links into admin/debug surfaces, forms with no CSRF token in sight.

Deliberately not an expression language. A rule is a fixed set of match keys
(exact string, or a named-field regex) ANDed together, plus optional page-level
CSRF-token gates — `yaml.safe_load` in, dict lookups + `re.search` out. There
is nothing to sandbox because there is nothing to evaluate: no code path turns
rule text into executable code.

# ponytail: add a match key here only when a real rule needs it, not on spec.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_RULES = Path(__file__).with_name("surfaces.yaml")

# Node match keys. Exact keys compare a node field case-insensitively; regex
# keys re.search a node field. Unknown keys in a rule are a hard error, not a
# silent no-op — a typo'd `lable_regex` that never fires is the worst outcome.
_EXACT_FIELDS = {"type", "role", "input_type"}
_REGEX_FIELDS = {
    "label_regex": "label",
    "href_regex": "href",
    "selector_regex": "selector",
    "value_regex": "value",
    "placeholder_regex": "placeholder",
}
_PRESENCE_KEYS = {"has", "missing"}  # value is a list of node-attribute names
_MATCH_KEYS = _EXACT_FIELDS | set(_REGEX_FIELDS) | _PRESENCE_KEYS
_PAGE_KEYS = {"present_hidden_regex", "missing_hidden_regex"}
_SEVERITIES = {"low", "medium", "high"}


@dataclass
class Rule:
    id: str
    severity: str
    description: str
    match: dict[str, Any]
    page: dict[str, Any]
    # Compiled once at load so evaluation over thousands of nodes never recompiles.
    _regex: dict[str, re.Pattern[str]] = field(default_factory=dict)

    def compile(self) -> "Rule":
        for key, pat in {**self.match, **self.page}.items():
            if key in _REGEX_FIELDS or key in _PAGE_KEYS:
                self._regex[key] = re.compile(pat, re.IGNORECASE)
        return self


@dataclass
class Finding:
    rule: str
    severity: str
    description: str
    url: str
    node_id: str | None = None
    label: str | None = None
    selector: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def load_rules(path: str | Path | None = None) -> list[Rule]:
    """Parse a rules YAML file. Raises ValueError with the offending id on any
    unknown or malformed key so a bad ruleset fails at load, not silently at scan."""
    path = Path(path) if path else DEFAULT_RULES
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = doc.get("rules", doc if isinstance(doc, list) else [])
    if not isinstance(raw, list):
        raise ValueError(f"{path}: top-level 'rules' must be a list")
    rules: list[Rule] = []
    for i, r in enumerate(raw):
        rid = r.get("id") or f"rule[{i}]"
        match = r.get("match", {}) or {}
        page = r.get("page", {}) or {}
        if unknown := (set(match) - _MATCH_KEYS):
            raise ValueError(f"{rid}: unknown match keys {sorted(unknown)}")
        if unknown := (set(page) - _PAGE_KEYS):
            raise ValueError(f"{rid}: unknown page keys {sorted(unknown)}")
        if not match and not page:
            raise ValueError(f"{rid}: rule has neither 'match' nor 'page' conditions")
        severity = r.get("severity", "medium")
        if severity not in _SEVERITIES:
            raise ValueError(f"{rid}: severity must be one of {sorted(_SEVERITIES)}")
        rules.append(
            Rule(rid, severity, r.get("description", ""), match, page).compile()
        )
    return rules


def _node_matches(rule: Rule, node: dict[str, Any]) -> bool:
    for key, expected in rule.match.items():
        if key in _EXACT_FIELDS:
            if str(node.get(key, "")).lower() != str(expected).lower():
                return False
        elif key in _REGEX_FIELDS:
            if not rule._regex[key].search(str(node.get(_REGEX_FIELDS[key], "") or "")):
                return False
        elif key == "has":
            if any(not node.get(attr) for attr in expected):
                return False
        elif key == "missing":
            if any(node.get(attr) for attr in expected):
                return False
    return True


def _page_ok(rule: Rule, hidden_blob: str) -> bool:
    """Whether the rule's page-level gate holds. A rule with no page keys always
    passes — the gate is opt-in."""
    if "present_hidden_regex" in rule.page and not rule._regex["present_hidden_regex"].search(hidden_blob):
        return False
    if "missing_hidden_regex" in rule.page and rule._regex["missing_hidden_regex"].search(hidden_blob):
        return False
    return True


def evaluate(graph: dict[str, Any], rules: list[Rule]) -> list[Finding]:
    """All findings for one parsed graph, in (rule order, node order)."""
    meta = graph.get("metadata", {})
    url = meta.get("url", "")
    hidden_blob = " ".join(h.get("name", "") for h in meta.get("hidden_fields", []))
    nodes = graph.get("nodes", [])
    findings: list[Finding] = []
    for rule in rules:
        if not _page_ok(rule, hidden_blob):
            continue
        if not rule.match:  # page-level-only rule: one finding for the page
            findings.append(Finding(rule.id, rule.severity, rule.description, url))
            continue
        for node in nodes:
            if _node_matches(rule, node):
                findings.append(
                    Finding(
                        rule.id, rule.severity, rule.description, url,
                        node_id=node.get("id"),
                        label=node.get("label"),
                        selector=node.get("selector"),
                    )
                )
    return findings


# --- Response-header posture rules ----------------------------------------
# A separate, equally-declarative engine: the node rules judge the DOM, these
# judge the HTTP response (CSP/CORS/Set-Cookie). Same `Finding` output, same
# YAML file under a `header_rules:` key. Keys: `header` (name, lowercased) plus
# exactly one condition — `absent` (fires if the header is missing), `present`
# (fires if present), `value_regex` (present AND matches), `value_missing_regex`
# (present AND does NOT match, e.g. Set-Cookie without SameSite).
_HEADER_CONDS = {"absent", "present", "value_regex", "value_missing_regex"}


@dataclass
class HeaderRule:
    id: str
    severity: str
    description: str
    header: str
    cond: str
    pattern: re.Pattern[str] | None = None


def load_header_rules(path: str | Path | None = None) -> list[HeaderRule]:
    """Parse the `header_rules:` section (same file as the node rules). A missing
    section is fine — returns []. A malformed rule fails loudly, like load_rules."""
    path = Path(path) if path else DEFAULT_RULES
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = doc.get("header_rules", []) if isinstance(doc, dict) else []
    if not isinstance(raw, list):
        raise ValueError(f"{path}: 'header_rules' must be a list")
    rules: list[HeaderRule] = []
    for i, r in enumerate(raw):
        rid = r.get("id") or f"header_rule[{i}]"
        if "header" not in r:
            raise ValueError(f"{rid}: header rule needs a 'header' name")
        conds = _HEADER_CONDS & set(r)
        if len(conds) != 1:
            raise ValueError(f"{rid}: header rule needs exactly one of {sorted(_HEADER_CONDS)}")
        cond = conds.pop()
        severity = r.get("severity", "medium")
        if severity not in _SEVERITIES:
            raise ValueError(f"{rid}: severity must be one of {sorted(_SEVERITIES)}")
        pat = None
        if cond in ("value_regex", "value_missing_regex"):
            pat = re.compile(str(r[cond]), re.IGNORECASE)
        rules.append(HeaderRule(rid, severity, r.get("description", ""),
                                r["header"].lower(), cond, pat))
    return rules


_CSRF_TOKEN_RE = re.compile(r"csrf|xsrf|authenticity_token|__requestverificationtoken|anti[-_]?forgery", re.I)
_SAMESITE_SAFE_RE = re.compile(r"samesite\s*=\s*(strict|lax)", re.I)


def csrf_posture(headers: dict[str, str], post_endpoints: list[str], hidden_blob: str,
                 url: str) -> Finding | None:
    """The finding the individual signals only hint at: a state-changing POST that
    is cookie-authenticated, with the cookie sendable cross-site (no SameSite
    Strict/Lax) and no anti-CSRF token in sight. Correlates what `evaluate_headers`,
    `extract_endpoints` and the hidden-field gate each see separately — a single
    missing-CSRF input is noise, this conjunction is the exploitable bug."""
    cookie = headers.get("set-cookie")
    if not cookie or _SAMESITE_SAFE_RE.search(cookie):
        return None  # no cookie session, or SameSite already blocks cross-site use
    if not post_endpoints or _CSRF_TOKEN_RE.search(hidden_blob or ""):
        return None  # nothing state-changing, or an anti-CSRF token is present
    shown = ", ".join(post_endpoints[:3]) + (" …" if len(post_endpoints) > 3 else "")
    return Finding(
        "csrf-posture", "medium",
        f"state-changing POST ({shown}) is cookie-authenticated with no SameSite and no "
        f"anti-CSRF token — forgeable from any origin",
        url)


def evaluate_headers(headers: dict[str, str], url: str, rules: list[HeaderRule]) -> list[Finding]:
    """Findings for one response's headers, in rule order. `headers` is lowercased
    keys → value (multiple Set-Cookie joined by newline), as `_http_fetch` returns."""
    findings: list[Finding] = []
    for rule in rules:
        val = headers.get(rule.header)
        fired = (
            (rule.cond == "absent" and val is None)
            or (rule.cond == "present" and val is not None)
            or (rule.cond == "value_regex" and val is not None and rule.pattern.search(val))
            or (rule.cond == "value_missing_regex" and val is not None and not rule.pattern.search(val))
        )
        if fired:
            findings.append(Finding(rule.id, rule.severity, rule.description, url))
    return findings


def _selfcheck() -> None:
    rules = load_rules()  # the shipped ruleset must parse
    graph = {
        "metadata": {"url": "http://x", "hidden_fields": [{"name": "session"}]},
        "nodes": [
            {"id": "node_01", "type": "input", "role": "textbox",
             "label": "API_KEY", "selector": "#k", "value": ""},
            {"id": "node_02", "type": "a", "role": "link",
             "label": "Panel", "selector": "#p", "href": "/admin/users"},
            {"id": "node_03", "type": "input", "role": "textbox",
             "label": "Search", "selector": "#s", "value": ""},
        ],
    }
    found = {f.rule for f in evaluate(graph, rules)}
    assert "sensitive-field-name" in found, found      # API_KEY label
    assert "exposed-admin-link" in found, found         # /admin/users href
    assert "form-without-csrf" not in found, found      # retired: too noisy, see csrf_posture
    # A rule with an unknown key must fail loudly.
    import tempfile
    bad = Path(tempfile.mkstemp(suffix=".yaml")[1])
    bad.write_text("rules:\n  - id: b\n    match: {lable_regex: x}\n")
    try:
        load_rules(bad)
        raise AssertionError("expected unknown-key rejection")
    except ValueError as exc:
        assert "unknown match keys" in str(exc), exc
    finally:
        bad.unlink()

    # Header rules: the shipped set parses, and fires on a weak posture.
    hrules = load_header_rules()
    weak = evaluate_headers(
        {"access-control-allow-origin": "*", "set-cookie": "session=abc; Path=/"},
        "http://x", hrules)
    hits = {f.rule for f in weak}
    assert "missing-csp" in hits, hits                 # no CSP header at all
    assert "cors-wildcard-origin" in hits, hits        # ACAO: *
    assert "cookie-without-samesite" in hits, hits      # Set-Cookie lacks SameSite
    strong = evaluate_headers(
        {"content-security-policy": "default-src 'self'",
         "set-cookie": "session=abc; Secure; HttpOnly; SameSite=Lax"},
        "http://x", hrules)
    assert not {f.rule for f in strong} & {"missing-csp", "cookie-without-samesite"}, strong

    # CSRF correlation: cookie w/o SameSite + a POST endpoint + no token → fires.
    assert csrf_posture({"set-cookie": "session=abc; Path=/"}, ["/api/transfer"], "", "http://x")
    assert csrf_posture({"set-cookie": "session=abc; SameSite=Lax"}, ["/api/transfer"], "", "http://x") is None
    assert csrf_posture({"set-cookie": "session=abc"}, ["/api/transfer"], "csrf_token", "http://x") is None
    assert csrf_posture({"set-cookie": "session=abc"}, [], "", "http://x") is None
    print("surfaces selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
