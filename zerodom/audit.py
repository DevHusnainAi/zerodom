"""Audit the selectors in an existing test suite against the running app.

A selector that matches nothing fails loudly and someone fixes it. A selector
that matches *two* elements passes, acts on whichever the engine reached first,
and produces a test that fails one run in twenty for reasons nobody can
reproduce. That is the defect this looks for.

Nothing here replaces the suite or requires ZeroDOM in it — it reads the
selectors already written, resolves each against a live page, and reports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Selector-bearing calls across the frameworks people actually have. Each
# pattern captures the selector as group 1 and is anchored on the call so a bare
# string in an assertion is never mistaken for one.
CALL_PATTERNS = [
    # Playwright / Puppeteer
    r"""\.locator\(\s*(['"`])(?P<sel>(?:(?!\1).)+)\1""",
    r"""\.(?:query_selector|querySelector|querySelectorAll|query_selector_all)\(\s*(['"`])(?P<sel>(?:(?!\1).)+)\1""",
    r"""\.wait_for_selector\(\s*(['"`])(?P<sel>(?:(?!\1).)+)\1""",
    r"""\.waitForSelector\(\s*(['"`])(?P<sel>(?:(?!\1).)+)\1""",
    r"""\bpage\.(?:click|fill|type|check|hover|focus|press)\(\s*(['"`])(?P<sel>(?:(?!\1).)+)\1""",
    # Cypress / Testing Library escape hatch
    r"""\bcy\.get\(\s*(['"`])(?P<sel>(?:(?!\1).)+)\1""",
    # Selenium
    r"""By\.CSS_SELECTOR\s*,\s*(['"`])(?P<sel>(?:(?!\1).)+)\1""",
    r"""By\.css\(\s*(['"`])(?P<sel>(?:(?!\1).)+)\1""",
]
COMPILED = [re.compile(p) for p in CALL_PATTERNS]

SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "dist", "build", "__pycache__"}

# A selector that is only a tag name matches half the page by design; flagging
# those as ambiguous would bury the real findings.
TOO_GENERIC = re.compile(r"^[a-z]+$")


@dataclass
class Finding:
    selector: str
    where: list[str] = field(default_factory=list)
    matches: int = -1

    @property
    def verdict(self) -> str:
        if self.matches < 0:
            return "invalid"
        if self.matches == 0:
            return "dead"
        if self.matches > 1:
            return "ambiguous"
        return "ok"


def extract(paths: Iterable[Path]) -> dict[str, list[str]]:
    """`selector -> ["file:line", ...]` for every selector-bearing call found."""
    found: dict[str, list[str]] = {}
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern in COMPILED:
                for match in pattern.finditer(line):
                    selector = match.group("sel").strip()
                    if not selector or TOO_GENERIC.match(selector):
                        continue
                    found.setdefault(selector, []).append(f"{path}:{lineno}")
    return found


def walk(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return [
        p
        for p in root.rglob("*")
        if p.suffix in SUFFIXES
        and p.is_file()
        and not SKIP_DIRS & set(p.parts)
    ]


def check(page: Any, selectors: dict[str, list[str]]) -> list[Finding]:
    """Resolve each selector against a live page and count what it matches."""
    findings = []
    for selector, where in selectors.items():
        finding = Finding(selector=selector, where=where)
        try:
            finding.matches = page.locator(selector).count()
        except Exception:
            finding.matches = -1
        findings.append(finding)
    # Ambiguous first: it is the one that fails silently.
    order = {"ambiguous": 0, "invalid": 1, "dead": 2, "ok": 3}
    findings.sort(key=lambda f: (order[f.verdict], -f.matches, f.selector))
    return findings


def report(findings: list[Finding], url: str) -> str:
    counts = {v: sum(f.verdict == v for f in findings) for v in ("ambiguous", "dead", "invalid", "ok")}
    lines = [f"Audited {len(findings)} selectors against {url}", ""]
    for verdict, blurb in (
        ("ambiguous", "match more than one element — a click may hit the wrong one"),
        ("invalid", "are not valid selectors for this engine"),
        ("dead", "match nothing on this page"),
    ):
        hits = [f for f in findings if f.verdict == verdict]
        if not hits:
            continue
        lines.append(f"{verdict.upper()}  {len(hits)} {blurb}")
        for f in hits:
            suffix = f"  ({f.matches} matches)" if f.matches > 1 else ""
            lines.append(f"  {f.selector}{suffix}")
            for w in f.where[:3]:
                lines.append(f"      {w}")
            if len(f.where) > 3:
                lines.append(f"      … and {len(f.where) - 3} more")
        lines.append("")
    lines.append(
        f"ambiguous {counts['ambiguous']} · dead {counts['dead']} · "
        f"invalid {counts['invalid']} · ok {counts['ok']}"
    )
    if not counts["ambiguous"]:
        lines.append("No ambiguous selectors. Nothing here will click the wrong element.")
    return "\n".join(lines)


def run(path: str, url: str, render_wait: int = 2500) -> tuple[int, str]:
    """Extract, resolve, report. Returns (ambiguous_count, report_text)."""
    from playwright.sync_api import sync_playwright

    files = walk(Path(path))
    selectors = extract(files)
    if not selectors:
        return 0, (
            f"No selectors found in {path}. Looked in {len(files)} files for "
            "locator(), query_selector(), cy.get(), By.css() and page.click()."
        )
    with sync_playwright() as pw:
        from .cli import _launch  # function-level: cli.main lazy-imports audit, avoid the cycle

        browser = _launch(pw, None)  # missing-Chromium → one-line install hint, not a traceback
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(render_wait)
        findings = check(page, selectors)
        browser.close()
    ambiguous = sum(f.verdict == "ambiguous" for f in findings)
    return ambiguous, report(findings, url)
