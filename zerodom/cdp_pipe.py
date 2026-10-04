"""Talk raw CDP to a freshly *spawned* Chrome over --remote-debugging-pipe
(FD 3 = commands in, FD 4 = events/responses out) — no localhost TCP port and
no Playwright driver process.

Why a pipe and not connect_over_cdp / :9222: a debugging port is a control
channel any local process can reach; the pipe's FDs are inherited only by our
own child, so nothing else on the box can drive the browser. And this path
carries no Playwright dependency, so `zerodom inspect --stealth <url>` can
spawn a throwaway-profile Chrome, read one page, and exit leaving no port open
and (with an ephemeral --user-data-dir) nothing on disk.

This SPAWNS Chrome; it does not attach to your already-running browser — a
pipe's FDs are fixed at launch. Driving your real, logged-in Chrome is what
the WS relay + extension (relay.py) is for.

# ponytail: POSIX only. On Windows Chrome exchanges the same protocol over two
# inherited OS HANDLEs passed as --remote-debugging-io-pipes rather than FDs
# 3/4; spawn() raises there instead of pretending. Add the Windows wiring when
# a Windows user actually needs stealth spawn.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import tempfile
import time
from typing import Any

from . import clearance
from .parser import _detect_challenge

# Chrome's pipe transport frames each JSON-RPC message with a single trailing
# NUL byte, in both directions. This is the wire contract — do not change it.
_DELIM = b"\0"

# First match wins. Covers the common Chrome/Chromium install names across
# Linux and macOS; override with the `chrome=` argument or $ZERODOM_CHROME.
_CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


class CDPError(RuntimeError):
    """A CDP command came back with an `error`, or the pipe died mid-exchange."""


def find_chrome(explicit: str | None = None) -> str:
    """Absolute path to a Chrome/Chromium binary, or raise with what was tried."""
    if explicit:
        found = shutil.which(explicit) or (explicit if os.path.exists(explicit) else None)
        if found:
            return found
        raise CDPError(f"chrome binary not found: {explicit!r}")
    env = os.environ.get("ZERODOM_CHROME")
    for candidate in ((env,) if env else ()) + _CHROME_CANDIDATES:
        found = shutil.which(candidate) or (candidate if os.path.exists(candidate) else None)
        if found:
            return found
    raise CDPError(
        "no Chrome/Chromium found; set $ZERODOM_CHROME or pass chrome=<path>. "
        f"Tried: {', '.join(_CHROME_CANDIDATES)}"
    )


class CDPPipe:
    """Synchronous CDP transport over one Chrome subprocess's FD 3/4 pair.

    Request/response is issued sequentially (`send` blocks until the matching
    id returns); protocol *events* seen while waiting are buffered on
    `self.events` rather than dropped, so callers that need `Page.loadEventFired`
    can still find it. That is all the concurrency this needs — one reader, one
    outstanding command — so there is no thread and no event loop.
    """

    def __init__(self, write_fd: int, read_fd: int, proc: subprocess.Popen[bytes], profile_dir: str | None):
        self._w = write_fd  # parent -> chrome (chrome reads this as its FD 3)
        self._r = read_fd  # chrome -> parent (chrome writes this as its FD 4)
        self._proc = proc
        self._profile_dir = profile_dir
        self._next_id = 0
        self._buf = b""
        self.events: list[dict[str, Any]] = []

    # -- protocol ---------------------------------------------------------

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send one command and return its `result`, buffering events meanwhile."""
        self._next_id += 1
        msg: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if session_id is not None:
            msg["sessionId"] = session_id
        try:
            os.write(self._w, json.dumps(msg).encode("utf-8") + _DELIM)
        except OSError as exc:  # broken pipe = Chrome already gone
            raise CDPError(f"write to chrome failed ({method}): {exc}") from exc
        return self._await(self._next_id, time.monotonic() + timeout)

    def _await(self, want_id: int, deadline: float) -> dict[str, Any]:
        while True:
            msg = json.loads(self._read_frame(deadline))
            if msg.get("id") == want_id:
                if "error" in msg:
                    raise CDPError(msg["error"])
                return msg.get("result", {})
            if "method" in msg:  # an event, or a command's response we don't await
                self.events.append(msg)

    def _read_frame(self, deadline: float) -> bytes:
        """One NUL-delimited frame, enforcing the deadline via select()."""
        while _DELIM not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for a CDP frame from chrome")
            ready, _, _ = select.select([self._r], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self._r, 65536)
            if not chunk:  # EOF: Chrome closed the pipe / exited
                raise CDPError("chrome closed the pipe")
            self._buf += chunk
        frame, _, self._buf = self._buf.partition(_DELIM)
        return frame

    # -- high level -------------------------------------------------------

    def page_session(self, timeout: float = 10.0) -> str:
        """Attach (flattened) to the first page target and return its sessionId.

        The page target can lag the browser-level connection by a few ms after
        launch, so poll `Target.getTargets` until one appears.
        """
        deadline = time.monotonic() + timeout
        while True:
            targets = self.send("Target.getTargets")["targetInfos"]
            page = next((t for t in targets if t.get("type") == "page"), None)
            if page is not None:
                return self.send(
                    "Target.attachToTarget",
                    {"targetId": page["targetId"], "flatten": True},
                )["sessionId"]
            if time.monotonic() > deadline:
                raise CDPError("no page target appeared")
            time.sleep(0.05)

    def outer_html(
        self, url: str | None, session_id: str, timeout: float = 30.0,
        headers: dict | None = None, cookies: list | None = None,
    ) -> tuple[str, str]:
        """Navigate (if `url` given), wait for load, return (outer_html, final_url).

        `headers` (a program's bypass header) and `cookies` (a cleared session
        reused from a storage_state file) are set over CDP before navigating.

        Load-wait and reads deliberately avoid the Runtime domain (rung-2
        hygiene): bot vendors fingerprint the `Runtime.enable` + evaluate
        pattern this path used to send mid-load (rebrowser-patches; DataDome's
        CDP-signal research), so load state comes from the Page lifecycle
        events and the HTML/URL from DOM.getOuterHTML + getNavigationHistory.

        After the read, the clearance ladder runs (choke point 3 of 3, see
        clearance.py) against a CDPPipe-backed page view; if it cleared the
        wall, the HTML returned is the cleared page's, not the interstitial's.
        """
        self.send("Page.enable", session_id=session_id)
        self.send("Page.setLifecycleEventsEnabled", {"enabled": True}, session_id=session_id)
        if headers or cookies:
            self.send("Network.enable", session_id=session_id)
        if headers:
            self.send("Network.setExtraHTTPHeaders", {"headers": headers}, session_id=session_id)
        if cookies:
            self.send("Network.setCookies", {"cookies": cookies}, session_id=session_id)
        # Drop any lifecycle events buffered before this navigation — a stale
        # "load" from the document we're leaving must not satisfy the wait.
        self._purge_lifecycle(session_id)
        loader_id = None
        if url:
            nav = self.send("Page.navigate", {"url": url}, session_id=session_id, timeout=timeout)
            if nav.get("errorText"):
                # Old behavior read whatever the error page rendered; the
                # bounded load-wait below keeps that (never hang a fetch).
                pass
            loader_id = nav.get("loaderId")
            self._wait_lifecycle(session_id, "load", loader_id, timeout)
        html, final_url = self._read_document(session_id)
        # Choke point 3 of 3: the ladder against a wall (it exits fast when
        # the page is clean and, being sync, runs its coroutine on this
        # thread's loop via clearance._run_sync). Re-read after any attempt —
        # rungs 1-3 re-navigate, so the interstitial we just read may be gone.
        policy = clearance.make_policy(transport="cdp_pipe", url=final_url)
        clearance.attempt_clearance(_PipePage(self, session_id), None, policy)
        if _detect_challenge(html) is not None:
            html, final_url = self._read_document(session_id)
        return html, final_url

    # -- lifecycle / document reads (no Runtime domain) --------------------

    def _purge_lifecycle(self, session_id: str) -> None:
        self.events[:] = [
            ev for ev in self.events
            if not (ev.get("method") == "Page.lifecycleEvent"
                    and ev.get("sessionId") == session_id)
        ]

    def _wait_lifecycle(
        self, session_id: str, name: str, loader_id: str | None, timeout: float,
    ) -> None:
        """Bounded wait for a Page.lifecycleEvent on this session/loader.

        Best-effort on timeout: like cli._goto's bounded networkidle, reading
        whatever is there beats failing the fetch — a challenge page that
        never fires load must still be readable (and flagged) by the parser.
        """
        deadline = time.monotonic() + timeout
        while True:
            for i, ev in enumerate(self.events):
                if self._is_lifecycle(ev, session_id, name, loader_id):
                    del self.events[i]
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                msg = json.loads(self._read_frame(deadline))
            except TimeoutError:
                return
            if self._is_lifecycle(msg, session_id, name, loader_id):
                return
            self.events.append(msg)

    @staticmethod
    def _is_lifecycle(msg: dict, session_id: str, name: str, loader_id: str | None) -> bool:
        if msg.get("method") != "Page.lifecycleEvent" or msg.get("sessionId") != session_id:
            return False
        params = msg.get("params") or {}
        if params.get("name") != name:
            return False
        return loader_id is None or params.get("loaderId") == loader_id

    def _read_document(self, session_id: str) -> tuple[str, str]:
        """(documentElement outerHTML, current URL) via the DOM + Page domains.

        The HTML element is matched by nodeType 1: a DOCTYPE's nodeName is
        also "html" (its doctype name), so nodeName alone picks the wrong node."""
        self.send("DOM.enable", session_id=session_id)
        root = self.send("DOM.getDocument", {"depth": 1}, session_id=session_id)
        node_id = None
        for child in (root.get("root") or {}).get("children") or []:
            if child.get("nodeType") == 1 and str(child.get("nodeName", "")).upper() == "HTML":
                node_id = child["nodeId"]
                break
        if node_id is None:  # no <html> yet (blank/failed doc) — read the root
            node_id = (root.get("root") or {}).get("nodeId")
        html = self.send(
            "DOM.getOuterHTML", {"nodeId": node_id}, session_id=session_id
        )["outerHTML"]
        frame_tree = self.send("Page.getFrameTree", session_id=session_id)
        final_url = (frame_tree.get("frameTree") or {}).get("frame", {}).get("url", "")
        return html, final_url

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Ask Chrome to quit, then tear down pipes and the ephemeral profile."""
        try:
            self.send("Browser.close", timeout=5.0)
        except (CDPError, TimeoutError, OSError):
            pass  # closing anyway
        for fd in (self._w, self._r):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        if self._profile_dir:
            shutil.rmtree(self._profile_dir, ignore_errors=True)

    def __enter__(self) -> CDPPipe:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _PipeContext:
    """The cookie-jar slice of a Playwright BrowserContext, over CDP — enough
    for clearance._carries_clearance / _add_cookies on the stealth transport."""

    def __init__(self, pipe: CDPPipe, session_id: str, url_getter):
        self._pipe = pipe
        self._sid = session_id
        self._url = url_getter

    def cookies(self) -> list[dict]:
        try:
            res = self._pipe.send("Network.getCookies", {}, session_id=self._sid)
        except CDPError:
            return []
        return res.get("cookies") or []

    def add_cookies(self, jar: list[dict]) -> None:
        self._pipe.send("Network.enable", session_id=self._sid)  # setCookie's domain
        for c in jar:
            params = {k: c[k] for k in
                      ("name", "value", "domain", "path", "expires", "httpOnly",
                       "secure", "sameSite") if k in c}
            if "url" not in params and "domain" not in params:
                params["url"] = self._url() or "about:blank"
            self._pipe.send("Network.setCookie", params, session_id=self._sid)


class _PipePage:
    """Duck-typed page view over CDPPipe for the clearance ladder — supplies
    exactly the methods clearance.py calls (content/goto/evaluate + context),
    sync, so the ladder's _aw/_run_sync plumbing drives it unchanged."""

    def __init__(self, pipe: CDPPipe, session_id: str):
        self._pipe = pipe
        self._sid = session_id
        self.url = ""
        self.context = _PipeContext(pipe, session_id, lambda: self.url)

    def content(self) -> str:
        html, self.url = self._pipe._read_document(self._sid)
        return html

    def goto(self, url: str, wait_until: str | None = None, timeout: float | None = None) -> str:
        """Page.navigate + bounded lifecycle wait — clearance._renav's contract
        (playwright semantics: wait_until in ms, raises on navigation failure)."""
        seconds = (timeout or 30000) / 1000
        # Purge before navigating, never after: a fast local page emits its
        # load event right behind the navigate response, and a purge in
        # between would drop the very event the wait below is waiting for —
        # burning the full timeout on a page that already loaded.
        self._pipe._purge_lifecycle(self._sid)
        nav = self._pipe.send("Page.navigate", {"url": url},
                              session_id=self._sid, timeout=seconds)
        if nav.get("errorText"):
            raise CDPError(f"navigation to {url} failed: {nav['errorText']}")
        # CDP's lifecycle names are camelCase ("DOMContentLoaded"), not the
        # Playwright spelling — waiting for "domcontentloaded" matches nothing
        # and burns the whole timeout on a page that already loaded.
        name = "DOMContentLoaded" if wait_until == "domcontentloaded" else "load"
        self._pipe._wait_lifecycle(self._sid, name, nav.get("loaderId"), seconds)
        _, self.url = self._pipe._read_document(self._sid)
        return self.url

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        """Run JS post-load. A bare Runtime.evaluate (no Runtime.enable — the
        domain stays un-enabled, so the mid-load fingerprint never happens);
        function-source + serialized-arg mirrors Playwright's evaluate(f, arg)."""
        expr = f"({expression})({json.dumps(arg)})" if arg is not None else expression
        res = self._pipe.send(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": True},
            session_id=self._sid,
        )
        if "exceptionDetails" in res:
            raise CDPError(str(res["exceptionDetails"].get("text") or "evaluate failed"))
        return res.get("result", {}).get("value")


def spawn(
    url: str | None = None,
    *,
    headless: bool = True,
    chrome: str | None = None,
    ephemeral_profile: bool = True,
    proxy: str | None = None,
    insecure: bool = False,
    extra_args: tuple[str, ...] = (),
) -> CDPPipe:
    """Spawn Chrome wired to a CDP pipe on FD 3/4 and return an open CDPPipe.

    FD wiring (POSIX): we create two pipes and, in the child just before exec,
    dup the parent->child read end onto 3 and the child->parent write end onto
    4 — the two descriptors `--remote-debugging-pipe` hardcodes. close_fds is
    off: with it on, CPython's fd-closing scan reclaims the just-freed slot 4
    for its own /proc/self/fd handle and Chrome sees "pipe fds not open". The
    dup targets survive exec because dup2 clears close-on-exec on them; the two
    pipe ends are the only extra fds Chrome inherits, and it ignores them.
    """
    if os.name != "posix":
        raise NotImplementedError(
            "cdp_pipe.spawn is POSIX-only (FD 3/4 wiring); Windows uses inherited "
            "HANDLEs. Use `zerodom inspect --render` (Playwright) there instead."
        )
    binary = find_chrome(chrome)

    cmd_r, cmd_w = os.pipe()  # commands: parent writes cmd_w, child reads cmd_r as FD 3
    evt_r, evt_w = os.pipe()  # events:   child writes evt_w as FD 4, parent reads evt_r
    os.set_inheritable(cmd_r, True)
    os.set_inheritable(evt_w, True)

    profile_dir = tempfile.mkdtemp(prefix="zerodom-cdp-") if ephemeral_profile else None
    args = [
        binary,
        "--remote-debugging-pipe",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        # Rung-2 hygiene: keep navigator.webdriver from announcing that this
        # spawn is automation-controlled (the pipe hides the port; this hides
        # the JS-visible half of the same signal).
        "--disable-blink-features=AutomationControlled",
    ]
    if headless:
        args.append("--headless=new")
    if profile_dir:
        args.append(f"--user-data-dir={profile_dir}")
    if proxy:
        # Route every request through the hunter's proxy (Burp/Caido). Chrome
        # bypasses the proxy for localhost by default, which would hide a local
        # target from the tool doing the intercepting — force it through too.
        args.append(f"--proxy-server={proxy}")
        args.append("--proxy-bypass-list=<-loopback>")
    if insecure:
        # Burp/Caido present their own CA; without this Chrome aborts the
        # intercepted TLS handshake. Explicit opt-in, off by default.
        args.append("--ignore-certificate-errors")
    args.extend(extra_args)
    if url:
        args.append(url)

    def _preexec() -> None:  # runs in the child after close_fds, before exec
        os.dup2(cmd_r, 3)
        os.dup2(evt_w, 4)

    try:
        proc = subprocess.Popen(
            args,
            preexec_fn=_preexec,
            close_fds=False,
            # CDP rides FD 3/4; Chrome's stdout/stderr carry only launch chatter
            # and close-time "could not write into pipe" noise. Keep our stdout
            # pure JSONL by dropping both.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        for fd in (cmd_r, cmd_w, evt_r, evt_w):
            try:
                os.close(fd)
            except OSError:
                pass
        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)
        raise

    # Parent keeps the command-write and event-read ends; the child owns the others.
    os.close(cmd_r)
    os.close(evt_w)
    return CDPPipe(write_fd=cmd_w, read_fd=evt_r, proc=proc, profile_dir=profile_dir)


def fetch(
    url: str,
    *,
    headless: bool = True,
    chrome: str | None = None,
    proxy: str | None = None,
    insecure: bool = False,
    headers: dict | None = None,
    cookies: list | None = None,
) -> tuple[str, str]:
    """One-shot: spawn, read (outer_html, final_url), clean up. The `--stealth` path.

    Launch blank, then navigate over CDP inside outer_html — passing the URL as a
    launch arg raced the page_session() attach, which grabbed the initial
    about:blank before the navigation committed and returned an empty graph.
    """
    with spawn(None, headless=headless, chrome=chrome, proxy=proxy, insecure=insecure) as pipe:
        return pipe.outer_html(url, pipe.page_session(), headers=headers, cookies=cookies)


def _selfcheck() -> None:
    """Framing round-trips over a real fd pair, no Chrome needed."""
    r, w = os.pipe()
    pipe = CDPPipe(write_fd=w, read_fd=r, proc=None, profile_dir=None)  # type: ignore[arg-type]
    # Two frames in one write, plus a partial third — exercises the buffer split.
    os.write(
        w,
        json.dumps({"method": "Some.event", "params": {}}).encode() + _DELIM
        + json.dumps({"id": 1, "result": {"ok": True}}).encode() + _DELIM
        + b'{"id":2,"result":',  # partial: no delimiter yet
    )
    result = pipe._await(1, time.monotonic() + 2.0)
    assert result == {"ok": True}, result
    assert pipe.events and pipe.events[0]["method"] == "Some.event"
    os.write(w, b'{"done":true}}' + _DELIM)
    assert pipe._await(2, time.monotonic() + 2.0) == {"done": True}
    os.close(w)
    os.close(r)
    print("cdp_pipe selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
