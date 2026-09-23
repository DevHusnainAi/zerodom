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
    assert "form-without-csrf" in found, found          # textbox + no csrf hidden
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
    print("surfaces selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
