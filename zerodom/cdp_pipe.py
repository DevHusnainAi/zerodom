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

    def outer_html(self, url: str | None, session_id: str, timeout: float = 30.0) -> tuple[str, str]:
        """Navigate (if `url` given), wait for load, return (outer_html, final_url)."""
        self.send("Page.enable", session_id=session_id)
        self.send("Runtime.enable", session_id=session_id)
        if url:
            self.send("Page.navigate", {"url": url}, session_id=session_id, timeout=timeout)
        # Block in-page until the document is fully loaded — one awaited promise
        # is simpler and race-free next to polling readyState from out here.
        self.send(
            "Runtime.evaluate",
            {
                "expression": (
                    "new Promise(r=>document.readyState==='complete'"
                    "?r():addEventListener('load',()=>r()))"
                ),
                "awaitPromise": True,
            },
            session_id=session_id,
            timeout=timeout,
        )
        evaluated = self.send(
            "Runtime.evaluate",
            {"expression": "document.documentElement.outerHTML", "returnByValue": True},
            session_id=session_id,
        )
        html = evaluated["result"]["value"]
        loc = self.send(
            "Runtime.evaluate",
            {"expression": "location.href", "returnByValue": True},
            session_id=session_id,
        )
        return html, loc["result"]["value"]

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


def spawn(
    url: str | None = None,
    *,
    headless: bool = True,
    chrome: str | None = None,
    ephemeral_profile: bool = True,
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
    ]
    if headless:
        args.append("--headless=new")
    if profile_dir:
        args.append(f"--user-data-dir={profile_dir}")
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


def fetch(url: str, *, headless: bool = True, chrome: str | None = None) -> tuple[str, str]:
    """One-shot: spawn, read (outer_html, final_url), clean up. The `--stealth` path."""
    with spawn(url, headless=headless, chrome=chrome) as pipe:
        return pipe.outer_html(None, pipe.page_session())


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
