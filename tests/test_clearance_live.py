"""Live end-to-end check for the clearance ladder (`clearance.attempt_clearance`).

Opt-in (needs a real headful Chrome + a display), never in CI:

    ZERODOM_BYPASS_LIVE=1 uv run pytest tests/test_clearance_live.py -v -s

`test_clearance.py` exercises every rung with fakes; this is the one test that
drives a *real browser* through the ladder, so a regression that only shows up
against live Chromium (DOM re-read timing, the poll window) gets caught. It
uses a *local synthetic* Cloudflare challenge we fully control — no third-party
host. The page carries genuine CF fingerprints, then (like a real managed
challenge) rewrites its own DOM after a beat; rung 1 "reused" out-waits it. A
real CF edge is validated by pointing `zerodom` at a page behind your *own* CF
account or an in-scope engagement target — same ladder, authorized target.

No solver fires here: no keys are set, so rung 3 no-ops — the clear is pure
local out-wait, which is also what keeps the default path zero-egress.
"""

import http.server
import os
import threading

import pytest

from zerodom import clearance

pytestmark = pytest.mark.skipif(
    not os.getenv("ZERODOM_BYPASS_LIVE"),
    reason="opt-in live clearance check (ZERODOM_BYPASS_LIVE=1, needs headful Chrome + display)",
)

# Initial response: a Cloudflare managed-challenge page (real `cf-chl` +
# `challenge-platform` fingerprints) whose own JS clears it after 800ms — swaps
# the body for post-challenge content with no markers left, exactly as CF's
# interstitial does once the browser check passes. 800ms < REUSE_WAIT_S (3.0s),
# so rung 1's bounded poll catches it.
CHALLENGE = """<!doctype html><html><head><title>Just a moment...</title></head>
<body>
  <div id="cf-chl-widget" class="challenge-platform">Checking if the site connection is secure</div>
  <script>
    setTimeout(function () {
      document.body.innerHTML = "<h1>Dashboard</h1><a href=/account>Account</a>";
    }, 800);
  </script>
</body></html>"""


@pytest.fixture
def challenge_url():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = CHALLENGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()


def test_managed_challenge_is_out_waited_by_rung_reused(challenge_url, monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")
    # No solver keys → rung 3 can't fire; this is a pure local out-wait (rung 1).
    for var in ("ZERODOM_SOLVER_PROVIDER", "ZERODOM_SOLVER_KEY", "ZERODOM_SOLVER_URL"):
        monkeypatch.delenv(var, raising=False)

    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=False)
        except Exception as exc:  # no chromium / no display
            pytest.skip(f"headful Chromium unavailable: {str(exc).splitlines()[0]}")
        page = browser.new_page()
        try:
            page.goto(challenge_url, wait_until="domcontentloaded")
            result = clearance.attempt_clearance(page, None, clearance.make_policy(url=challenge_url))
            cleared_html = page.content()
        finally:
            browser.close()

    assert result is not None, "the ladder should clear a self-resolving CF challenge"
    assert result["rung"] == "reused" and result["wall_kind"] == "cloudflare"
    assert clearance.take_cleared() == result  # recorded for the one parse that consumes it
    assert clearance.take_cleared() is None    # …and only once
    assert "Dashboard" in cleared_html  # the post-challenge DOM the ladder out-waited
