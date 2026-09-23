from zerodom.jsintel import scan_js


HTML = """
<html><body>
  <a href="/api/public">a visible link, not JS</a>
  <script>
    const AWS = "AKIAIOSFODNN7EXAMPLE";
    const jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdef123456";
    fetch("/api/v1/admin/refund");
    axios.get("/internal/flags");
    fetch("/api/v1/admin/refund");   // dup, should collapse
  </script>
</body></html>
"""


def test_secrets_and_endpoints_are_found():
    rules = [f.rule for f in scan_js(HTML, "https://t.example")]
    assert "js-secret:aws-access-key" in rules
    assert "js-secret:jwt" in rules


def test_endpoints_come_from_js_not_visible_links():
    eps = sorted(f.description.split(": ")[1] for f in scan_js(HTML, "u") if f.rule == "js-endpoint")
    assert eps == ["/api/v1/admin/refund", "/internal/flags"]  # deduped, /api/public excluded


def test_secret_value_is_redacted():
    # A finding must never reprint the raw key — the scan log is not a new leak.
    assert all("AKIAIOSFODNN7EXAMPLE" not in f.description for f in scan_js(HTML, "u"))


def test_output_is_deterministic():
    assert scan_js(HTML, "u") == scan_js(HTML, "u")
