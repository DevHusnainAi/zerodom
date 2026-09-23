import json
import pytest
from pathlib import Path

from zerodom import cli

PAGE = """
<html><title>Shop</title><body>
  <a href="/cart">View cart</a>
  <button>Complete checkout</button>
  <input name="q" aria-label="Search products">
</body></html>
"""


@pytest.fixture
def served(monkeypatch):
    """Skip the network: the CLI's only job here is what it does with the HTML."""
    monkeypatch.setattr(
        cli, "fetch",
        lambda url, render, stealth=False, proxy=None, insecure=False,
        headers=None, storage_state=None: (PAGE, url),
    )


def test_proxy_routes_the_plain_http_fetch():
    """--proxy must actually send the request through the proxy, not around it —
    the whole point is that Burp/Caido sees the traffic."""
    import http.server, threading
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = b"<html><title>ok</title></html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        html, _ = cli.fetch("http://example.com/", render=False,
                            proxy=f"http://127.0.0.1:{srv.server_address[1]}")
    finally:
        srv.shutdown()
    assert hits == ["http://example.com/"]
    assert "ok" in html


def test_frames_and_stealth_cannot_combine():
    with pytest.raises(SystemExit):
        cli.main(["inspect", "--frames", "--stealth", "https://example.com"])


def test_compact_is_the_default_output(served, capsys):
    cli.main(["https://shop.test/"])
    out = capsys.readouterr().out
    assert "[02] button 'Complete checkout'" in out
    assert '"selector"' not in out, "JSON must be opt-in"
    assert "compact text" in out


def test_json_flag_emits_the_full_graph(served, capsys):
    cli.main(["https://shop.test/", "--json"])
    cap = capsys.readouterr()
    # stdout is pure JSON so `--json | jq` works; the token report goes to stderr.
    assert '"selector":' in cap.out
    json.loads(cap.out)
    assert "(json)" in cap.err and "(json)" not in cap.out


def test_find_prints_only_matching_nodes(served, capsys):
    cli.main(["https://shop.test/", "--find", "checkout"])
    out = capsys.readouterr().out
    assert "[02] button 'Complete checkout'" in out
    assert "View cart" not in out


def test_find_matches_every_term(served, capsys):
    cli.main(["https://shop.test/", "--find", "input search"])
    out = capsys.readouterr().out
    assert "[03] input 'Search products'" in out
    assert "button" not in out.split("─")[0]


def test_find_reports_a_miss_without_dumping_the_graph(served, capsys):
    cli.main(["https://shop.test/", "--find", "delete account"])
    out = capsys.readouterr().out
    assert "No node matches 'delete account' among 3 nodes." in out
    assert "View cart" not in out


def test_the_inspect_verb_is_optional(served, capsys):
    """`inspect` is the only verb, so requiring it is ceremony — but it must still work."""
    cli.main(["https://shop.test/"])
    bare = capsys.readouterr().out
    cli.main(["inspect", "https://shop.test/"])
    verbose = capsys.readouterr().out
    # Everything but the summary, whose measured latency differs run to run.
    assert verbose.split("─")[0] == bare.split("─")[0]


def test_a_local_path_is_accepted_as_a_target(tmp_path, capsys):
    page = tmp_path / "page.html"
    page.write_text(PAGE, encoding="utf-8")
    cli.main([str(page)])
    out = capsys.readouterr().out
    assert "[01] a 'View cart'" in out
    assert page.as_uri() in out


def test_audit_is_a_first_class_verb():
    """`zerodom audit` must not be swallowed by the bare-URL shorthand."""
    import pytest

    from zerodom import cli

    with pytest.raises(SystemExit):
        cli.main(["audit", "--help"])


def test_extension_prints_a_loadable_directory(capsys):
    assert cli.main(["extension"]) == 0
    assert (Path(capsys.readouterr().out.strip()) / "manifest.json").is_file()


def test_header_and_storage_state_reach_the_server():
    """--header sends a program's bypass token; --storage-state carries a
    cleared-session cookie (cf_clearance) into the request."""
    import http.server, threading, json, tempfile, os
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["xbb"] = self.headers.get("X-Bug-Bounty")
            seen["cookie"] = self.headers.get("Cookie")
            body = b"<html><title>ok</title></html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    st = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"cookies": [{"name": "cf_clearance", "value": "abc"}], "origins": []}, st)
    st.close()
    try:
        cli.fetch(f"http://127.0.0.1:{srv.server_address[1]}/", render=False,
                  headers={"X-Bug-Bounty": "h1-42"}, storage_state=st.name)
    finally:
        srv.shutdown()
        os.unlink(st.name)
    assert seen["xbb"] == "h1-42"
    assert seen["cookie"] == "cf_clearance=abc"


def test_bad_header_is_a_clear_error():
    with pytest.raises(SystemExit):
        cli.main(["inspect", "--header", "no-colon-here", "https://example.com"])


def _state_file(cookie):
    import json, tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"cookies": [{"name": "session", "value": cookie}], "origins": []}, f)
    f.close()
    return f.name


def test_compare_flags_identical_body_as_possible_idor():
    """B's private resource returned byte-identical to A → cross-tenant IDOR tell."""
    import http.server, threading, os

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            # The vuln: /invoice/2 ignores auth and returns Bob's invoice to anyone.
            more = b"<a href=/admin>Admin</a>" if "admin" in self.headers.get("Cookie", "") else b"<a href=/me>Me</a>"
            body = (b"<html><body><h1>Invoice 2 - Bob</h1></body></html>"
                    if self.path == "/invoice/2" else b"<html><body>" + more + b"</body></html>")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    a, b = _state_file("attacker"), _state_file("admin")
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        idor = cli.compare_identities(f"{base}/invoice/2", [("A", a), ("B", b)])
        priv = cli.compare_identities(f"{base}/dash", [("A", a), ("B", b)])
    finally:
        srv.shutdown()
        os.unlink(a)
        os.unlink(b)
    assert idor["identical_body_pairs"] == [["A", "B"]]
    assert "IDOR" in idor["note"]
    assert priv["identical_body_pairs"] == []          # different bodies, isolated
    assert "Admin" in priv["only_B"]                    # admin sees more


def test_compare_needs_two_identities():
    with pytest.raises(SystemExit):
        cli.main(["compare", "https://x", "--as", "A=/does/not/exist.json"])
