#!/usr/bin/env python3
"""A deliberately broken tiny app for the `zerodom sweep` demo — stdlib only.

Three routes:
  /          public homepage, links to /admin and /account (echoes the user cookie
             so it differs per identity — a public page must NOT look like an IDOR).
  /account   per-user data, authorization enforced correctly (echoes the user) —
             the sweep must leave this alone (both authorized, different bodies).
  /admin     authorization enforced by a CLIENT-CONTROLLABLE cookie (Admin=true) —
             the bug. Returns 200 with the cookie, 401 without it.

Run:  python3 proof/sweep_demo/app.py   (serves on 127.0.0.1:8799)
"""
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8799


def _cookies(header: str) -> dict:
    c = SimpleCookie()
    c.load(header or "")
    return {k: v.value for k, v in c.items()}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, status: int, body: str):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        ck = _cookies(self.headers.get("Cookie", ""))
        user = ck.get("user", "guest")
        if self.path.rstrip("/") == "" or self.path == "/":
            self._send(200, f"<title>Acme</title><h1>Signed in as {user}</h1>"
                            "<a href='/account'>Your account</a> "
                            "<a href='/admin'>Admin panel</a>")
        elif self.path.startswith("/account"):
            self._send(200, f"<title>Account</title><h1>Account for {user}</h1>"
                            f"<p>Email: {user}@acme.test</p>")
        elif self.path.startswith("/admin"):
            if ck.get("Admin") == "true":
                self._send(200, "<title>Admin</title><h1>Admin panel</h1>"
                                "<a href='/admin/delete'>Delete a user</a>")
            else:
                self._send(401, "<title>Denied</title><h1>Access denied</h1>")
        else:
            self._send(404, "<title>404</title>not found")


if __name__ == "__main__":
    print(f"sweep-demo app on http://127.0.0.1:{PORT}")
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
