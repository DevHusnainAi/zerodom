"""Engagement scope, shared: deny-regex + host-allowlist, sync and throttle-free.

Extracted from `mcp_server._scope_ok` so the CLI clearance ladder gates rung 3
the same way MCP gates every action (the MCP wrapper stays async because of its
max_rps throttle — the core check itself never needed to be). Same semantics as
before the extraction: no scope dict at all means unrestricted.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from urllib.parse import urlparse

# Destructive/irreversible actions an unattended hunt must never take. Matched
# against a URL and against a node's href+label, case-insensitively.
DEFAULT_DENY = r"logout|sign-?out|/delete|/remove|/destroy|revoke|deactivate|/api/.*(delete|remove)|\bdelete\b|\bremove\b"


def check_scope(url: str, scope: dict | None) -> None:
    """Raise PermissionError when `url` falls outside `scope`; return None otherwise.

    `scope` is {allow: [host globs], deny: regex} — `None`/empty is unrestricted.
    """
    if not scope:
        return
    if scope.get("deny") and re.search(scope["deny"], url, re.I):
        raise PermissionError(f"scope: refusing {url!r} — matches the destructive deny rule.")
    host = urlparse(url).netloc.lower()
    allow = scope.get("allow") or []
    if host and allow and not any(fnmatch.fnmatch(host, p) for p in allow):
        raise PermissionError(f"scope: {host} is out of scope (allowed: {allow}).")


def load_scope(path: str) -> dict:
    """A ZERODOM_SCOPE YAML/JSON file → the scope dict. `locked` because the
    agent must not be able to widen a bound an operator wrote down."""
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {
        "allow": [h.lower() for h in (data.get("allow") or data.get("hosts") or [])],
        "deny": data.get("deny") or DEFAULT_DENY,
        "max_rps": data.get("max_rps"),
        "locked": True,
        "_last": 0.0,
    }
