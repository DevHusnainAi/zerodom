"""Adapter test — skips cleanly where crawl4ai isn't installed (it's optional)."""

import pytest

pytest.importorskip("crawl4ai")  # not a zerodom dep; adapter only loads with it

from zerodom.crawl4ai import ZeroDOMExtractionStrategy

HTML = """
<form action="/login">
  <a href="/forgot">Log in</a>
  <input type="password" name="pw" value="hunter2">
  <button type="submit">Sign in</button>
</form>
"""


def test_extract_returns_snapshot_and_refmap():
    [block] = ZeroDOMExtractionStrategy().extract("https://example.com/login", HTML)
    assert block["tags"] == ["zerodom.snapshot"]
    assert "Sign in" in block["content"]
    assert "hunter2" not in block["content"]  # sensitive value redacted to `filled`
    # ref_map keys match the [NN] refs in the text, and selectors stay out of content
    assert block["ref_map"] and all(r in block["content"] for r in block["ref_map"])
    assert "body >" not in block["content"]


def test_with_selectors_false_drops_refmap():
    [block] = ZeroDOMExtractionStrategy(with_selectors=False).extract("x", HTML)
    assert "ref_map" not in block
