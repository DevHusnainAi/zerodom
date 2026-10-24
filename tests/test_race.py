"""`zerodom race` against a server with a real check-then-act window.

Proves the burst wins a TOCTOU race (more than one identical concurrent request
succeeds on a single-use action) and that a properly-locked endpoint resists.
Plain HTTP + localhost so there is no TLS/cert setup."""

import argparse
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from zerodom import cli


def _ns(**kw):
    base = dict(url=None, method="POST", body=None, proxy=None, insecure=False,
                storage_state=None, headers=None, n=20, setup=None, check=None,
                engine="auto", fail_on_finding=False, tamper=None, save=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _massassign_server(vulnerable: bool):
    import json as _json

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = _json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            # Vulnerable server echoes whatever was sent (incl. an injected price);
            # the safe one only ever returns the fixed price.
            out = {"ok": True, **body} if vulnerable else {"ok": True, "price": 999}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(_json.dumps(out).encode())

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _server(locked: bool):
    state = {"applied": 0}
    lock = threading.Lock()

    def apply_once() -> int:
        # The classic check-then-act: read, pause, then write. Without the lock the
        # pause lets many concurrent requests pass the `== 0` check before any write.
        if state["applied"] == 0:
            time.sleep(0.05)
            state["applied"] += 1
            return 200
        return 409

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if locked:
                with lock:
                    code = apply_once()
            else:
                code = apply_once()
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


def test_race_burst_wins_a_real_toctou():
    srv, state = _server(locked=False)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/apply"
        statuses = cli._race_burst(url, [("POST", "{}")] * 20, {}, jar=None,
                                   storage_state=None, insecure=False)
    finally:
        srv.shutdown()
    ok = sum(1 for s in statuses if 200 <= s < 300)
    assert ok >= 2, (ok, statuses)          # the window let duplicates through
    assert state["applied"] >= 2            # server state confirms the over-apply


def test_race_burst_respects_a_lock():
    srv, state = _server(locked=True)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/apply"
        statuses = cli._race_burst(url, [("POST", "{}")] * 20, {}, jar=None,
                                   storage_state=None, insecure=False)
    finally:
        srv.shutdown()
    ok = sum(1 for s in statuses if 200 <= s < 300)
    assert ok == 1, (ok, statuses)          # exactly one wins; the rest 409
    assert state["applied"] == 1


def test_pick_sender_selects_by_availability_and_flag(monkeypatch):
    monkeypatch.setattr(cli, "_httpx_available", lambda: True)
    assert type(cli._pick_sender(_ns(engine="http1"), {})).__name__ == "Http1Sender"
    assert type(cli._pick_sender(_ns(engine="auto"), {})).__name__ == "Http2Sender"
    assert type(cli._pick_sender(_ns(engine="http2"), {})).__name__ == "Http2Sender"
    monkeypatch.setattr(cli, "_httpx_available", lambda: False)
    assert type(cli._pick_sender(_ns(engine="auto"), {})).__name__ == "Http1Sender"
    with pytest.raises(SystemExit):           # explicit http2 without the extra
        cli._pick_sender(_ns(engine="http2"), {})


def test_http2_engine_wins_local_race():
    pytest.importorskip("httpx")
    srv, state = _server(locked=False)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/apply"
        statuses, _ = cli.Http2Sender(_ns(url=url), {}).run([], [("POST", "{}")] * 20, None)
    finally:
        srv.shutdown()
    ok = sum(1 for s in statuses if 200 <= s < 300)
    assert ok >= 2, (ok, statuses)            # async burst path wins the open window


def test_mixed_body_burst_reports_per_body():
    """Two bodies fired together: both succeed on the open window; the stdlib burst
    returns statuses aligned to the interleaved specs."""
    srv, state = _server(locked=False)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/apply"
        specs = [("POST", '{"a":1}'), ("POST", '{"b":2}')] * 10
        statuses = cli._race_burst(url, specs, {}, jar=None, storage_state=None, insecure=False)
    finally:
        srv.shutdown()
    assert len(statuses) == len(specs)
    assert sum(1 for s in statuses if 200 <= s < 300) >= 2


def test_rate_limiter_spaces_calls(monkeypatch):
    import zerodom.cli as c
    clock = {"t": 0.0}
    slept = []
    monkeypatch.setattr(c, "_Rate", c._Rate)  # ensure real class
    import time as _t
    monkeypatch.setattr(_t, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(_t, "sleep", lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)))
    r = c._Rate(5)   # 5 rps -> 0.2s spacing
    r.wait(); r.wait(); r.wait()
    assert slept and all(abs(s - 0.2) < 1e-9 for s in slept), slept
    assert c._Rate(None).min == 0.0   # disabled


def test_probe_flags_mass_assignment(capsys):
    srv = _massassign_server(vulnerable=True)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/create"
        rc = cli.probe_cmd(_ns(url=url, body='{"name":"x"}', tamper='{"price":1}',
                               fail_on_finding=True))
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert rc == 1 and '"mass-assignment"' in out, out


def test_probe_quiet_when_field_ignored(capsys):
    srv = _massassign_server(vulnerable=False)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/create"
        rc = cli.probe_cmd(_ns(url=url, body='{"name":"x"}', tamper='{"price":1}',
                               fail_on_finding=True))
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert rc == 0 and "mass-assignment" not in out, out


def test_evidence_har_roundtrips(tmp_path):
    srv = _massassign_server(vulnerable=True)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/create"
        har = tmp_path / "ev.har"
        cli.probe_cmd(_ns(url=url, body='{"name":"x"}', tamper='{"price":1}', save=str(har)))
        import json
        doc = json.loads(har.read_text())
    finally:
        srv.shutdown()
    entries = doc["log"]["entries"]
    assert len(entries) == 2  # baseline + tampered
    assert entries[1]["request"]["postData"]["text"] == '{"name": "x", "price": 1}'
    assert entries[1]["response"]["status"] == 200


def test_evidence_http_format(tmp_path):
    srv = _massassign_server(vulnerable=True)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/create"
        out = tmp_path / "ev.http"
        cli.probe_cmd(_ns(url=url, body='{"name":"x"}', tamper='{"price":1}', save=str(out)))
        text = out.read_text()
    finally:
        srv.shutdown()
    assert "### exchange 1 — request" in text and "POST " in text
    assert "### exchange 2 — request" in text and '"price": 1' in text
    assert "### exchange 2 — response" in text and "HTTP/1.1 200" in text
