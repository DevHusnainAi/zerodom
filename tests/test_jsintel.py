from zerodom.jsintel import extract_endpoints, scan_js, scan_sinks


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
    descs = sorted(f.description for f in scan_js(HTML, "u") if f.rule == "js-endpoint")
    # Call sites are reported with their method; deduped; /api/public (a visible
    # href, not in a <script>) is excluded.
    assert descs == [
        "GET /api/v1/admin/refund (called in JS)",
        "GET /internal/flags (called in JS)",
    ], descs


def test_secret_value_is_redacted():
    # A finding must never reprint the raw key — the scan log is not a new leak.
    assert all("AKIAIOSFODNN7EXAMPLE" not in f.description for f in scan_js(HTML, "u"))


def test_output_is_deterministic():
    assert scan_js(HTML, "u") == scan_js(HTML, "u")


APP_JS = r"""
    const escapeHtml = v => String(v).replace(/[&<>'"]/g, c => c);
    async function addToCart(productId){
      await fetch("/api/cart/items", {method:"POST", body: JSON.stringify({ productId })});
    }
    async function applyCoupon(code){
      await fetch("/api/cart/coupon", {method:"POST", body: JSON.stringify({ code })});
    }
    function cartItem(item){
      return `<a class="thumb image-${item.image}" href="/x"></a>
              <span>${escapeHtml(item.name)}</span>`;
    }
"""


def test_extract_endpoints_method_and_body_shape():
    eps = {(e["method"], e["path"]): e["body_keys"] for e in extract_endpoints(APP_JS)}
    assert eps[("POST", "/api/cart/items")] == ["productId"]
    assert eps[("POST", "/api/cart/coupon")] == ["code"]


def test_external_bundle_scanned_when_passed():
    descs = " | ".join(f.description for f in scan_js("<html></html>", "u", scripts=[APP_JS]))
    assert "POST /api/cart/coupon body={code}" in descs
    assert "POST /api/cart/items body={productId}" in descs


def test_dom_sink_flags_unescaped_only():
    sinks = scan_sinks(APP_JS, "u")
    # item.image is raw inside an HTML template → one finding; the
    # escapeHtml(item.name) interpolation is safe and not reported. It is fed by
    # server data (no attacker source), so it is low, not high.
    assert len(sinks) == 1 and sinks[0].severity == "low"
    assert "item.image" in sinks[0].description


def test_dom_sink_reachable_from_source_is_high():
    js = ('let frag = new URLSearchParams(location.search).get("q");\n'
          'el.innerHTML = `<p>${frag}</p>`;')
    sinks = scan_sinks(js, "u")
    assert len(sinks) == 1 and sinks[0].severity == "high", [s.description for s in sinks]
    assert "attacker-controllable" in sinks[0].description
