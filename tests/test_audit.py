"""The audit reads someone else's test suite, so extraction is the risky half:
a missed selector is a defect it silently fails to report."""

from pathlib import Path

import pytest

from zerodom import audit
from zerodom.audit import Finding, check, extract, report, walk

SUITE = """
def test_login(page):
    page.click("#submit")
    page.fill('input[type=email]', "a@b.c")
    page.locator(".btn").click()
    page.wait_for_selector("#late")
    cy.get('[data-test=row]').click()
    driver.find_element(By.CSS_SELECTOR, ".legacy")
    await page.querySelector(`#tpl-quoted`)
    expect(page.locator("#only-one")).toBeVisible()
    assert "not a selector" in body
    page.click("div")
"""


def write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_extracts_across_frameworks_and_quote_styles(tmp_path):
    found = extract([write(tmp_path, "suite.py", SUITE)])
    assert set(found) == {
        "#submit",
        "input[type=email]",
        ".btn",
        "#late",
        "[data-test=row]",
        ".legacy",
        "#tpl-quoted",
        "#only-one",
    }


def test_bare_strings_and_tag_only_selectors_are_ignored():
    """`"not a selector"` is prose and `div` matches half the page by design —
    reporting either would bury the findings that matter."""
    found = extract([])
    assert found == {}


def test_records_every_call_site(tmp_path):
    body = 'page.click("#a")\npage.click("#a")\npage.locator("#a")\n'
    found = extract([write(tmp_path, "t.js", body)])
    assert len(found["#a"]) == 3
    assert all(w.endswith((":1", ":2", ":3")) for w in found["#a"])


def test_walk_skips_vendored_trees(tmp_path):
    (tmp_path / "node_modules").mkdir()
    write(tmp_path, "node_modules/dep.js", 'page.click("#x")')
    write(tmp_path, "real.spec.ts", 'page.click("#y")')
    write(tmp_path, "notes.md", 'page.click("#z")')
    assert [p.name for p in walk(tmp_path)] == ["real.spec.ts"]


class FakeLocator:
    def __init__(self, n):
        self.n = n

    def count(self):
        if self.n < 0:
            raise ValueError("bad selector")
        return self.n


class FakePage:
    """Resolution counts keyed by selector, so check() is testable without a browser."""

    def __init__(self, counts):
        self.counts = counts

    def locator(self, selector):
        return FakeLocator(self.counts[selector])


def test_verdicts_and_ambiguous_ranked_first():
    page = FakePage({".btn": 3, "#gone": 0, "#ok": 1, "bad(": -1})
    findings = check(page, {"#ok": ["a:1"], "#gone": ["b:2"], ".btn": ["c:3"], "bad(": ["d:4"]})
    assert [f.verdict for f in findings] == ["ambiguous", "invalid", "dead", "ok"]


def test_report_names_the_failure_mode():
    findings = check(FakePage({".btn": 4}), {".btn": ["tests/a.py:9"]})
    text = report(findings, "https://app.test")
    assert "may hit the wrong one" in text
    assert ".btn  (4 matches)" in text
    assert "tests/a.py:9" in text


def test_clean_suite_says_so():
    text = report(check(FakePage({"#ok": 1}), {"#ok": ["a:1"]}), "https://app.test")
    assert "No ambiguous selectors" in text


def test_finding_verdict_boundaries():
    assert Finding("x", matches=-1).verdict == "invalid"
    assert Finding("x", matches=0).verdict == "dead"
    assert Finding("x", matches=1).verdict == "ok"
    assert Finding("x", matches=2).verdict == "ambiguous"


def test_missing_chromium_gives_the_one_line_hint_not_a_traceback(tmp_path, monkeypatch):
    """audit routes its launch through cli._launch, so a first-run without
    Chromium installed prints the one actionable line every other browser
    subcommand gives — not a raw 10-line Playwright traceback."""
    suite = tmp_path / "t.py"
    suite.write_text('def test(page):\n    page.click("#submit")\n')

    class FakeChromium:
        def launch(self, **kw):
            raise RuntimeError("Executable doesn't exist at /root/.cache/ms-playwright/...")

    class FakePw:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakePw())

    with pytest.raises(SystemExit) as exc:
        audit.run(str(suite), "https://app.test")
    assert "playwright install chromium" in str(exc.value)
