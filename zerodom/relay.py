"""Local WS bridge between zerodom's own connect_over_cdp and zerodom's own
Chrome extension (extension/, see docs/DECISIONS.md D12). This file started
as a Python port of Microsoft's relay (cdpRelayV2.ts + browserModel.ts in the
microsoft/playwright monorepo) — same wire protocol, so the extension side
(D12) could be swapped in later without touching this file at all.

Two roles, kept as separate as the reference keeps them:
- BrowserModel: pure state machine, no I/O of its own. Owns the mapping
  between chrome tab ids and CDP session ids, and translates between the
  chrome.* dialect the extension speaks and the CDP dialect Playwright
  speaks. Everything it needs to send anywhere comes in as an injected
  callable, which is what makes it testable without a real browser or a real
  WebSocket (tests/test_relay.py).
- RelayServer: the actual WS server, two endpoints (one for us, one for the
  extension), wired to one BrowserModel per connected extension.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import websockets

logger = logging.getLogger("zerodom.relay")

SendToExtension = Callable[[str, Any], Awaitable[Any]]
SendToCDPClient = Callable[[dict[str, Any]], None]

# chrome.debugger.attach refuses these schemes/pages; auto-attaching one used to
# fail mid-handshake and wedge the whole relay session (an open chrome://extensions
# tab did it). about:blank and a fresh blank tab are drivable, so they stay in.
_UNATTACHABLE_SCHEMES = (
    "chrome://", "chrome-extension://", "chrome-untrusted://",
    "devtools://", "edge://", "view-source:", "about:",
)


def _is_attachable(tab: dict[str, Any]) -> bool:
    """Whether chrome.debugger can attach to a tab, by its URL. Un-attachable
    tabs are skipped rather than attempted, so one can't kill the session."""
    url = (tab.get("url") or tab.get("pendingUrl") or "").strip().lower()
    if url in ("", "about:blank"):
        return True
    if url.startswith(_UNATTACHABLE_SCHEMES):
        return False
    # The Chrome Web Store blocks the debugger too, over normal https.
    return "chromewebstore.google.com" not in url and "chrome.google.com/webstore" not in url


@dataclass
class TabSession:
    tab_id: int
    session_id: str  # "pw-tab-N", assigned by us, never by the extension
    target_info: dict[str, Any] | None
    # Child CDP sessions (workers, oopifs) belonging to this tab, tracked via
    # Target.attachedToTarget / Target.detachedFromTarget events on it.
    child_sessions: set[str] = field(default_factory=set)
    # Which BrowserModel._cdp_generation last got Target.attachedToTarget for
    # this tab (0 = none yet). Lets _attach_tab tell apart the two cache-hit
    # cases that look identical but need opposite handling: a genuinely new
    # CDP client replaying an already-attached tab (must re-emit, or that
    # client sees zero tabs) vs. two racing attachers *within* the same
    # client's session for a brand-new tab (must NOT re-emit — the client
    # already got Target.attachedToTarget once, and a second one for the
    # same targetId is a protocol violation Playwright's driver kills the
    # connection over).
    announced_generation: int = 0


class BrowserModel:
    """One instance per connected extension. Observation-only until
    connect_cdp_client() is called — Target.attachedToTarget etc. emitted
    before that would go nowhere, so they're just dropped rather than
    queued, matching the reference (cdpRelayV2.ts's ready-gate exists
    precisely so this observation-only window is never actually raced)."""

    def __init__(self, send_to_extension: SendToExtension):
        self._send_to_extension = send_to_extension
        self._send_to_cdp_client: SendToCDPClient | None = None
        self._known_tabs: dict[int, dict[str, Any]] = {}
        self._tab_sessions: dict[int, TabSession] = {}
        self._auto_attach = False
        self._next_session_id = 1
        # create_target() attaches the tab it just created directly, but that
        # same chrome.tabs.create *also* fires a real chrome.tabs.onCreated
        # event, which independently schedules on_tab_created()'s own
        # auto-attach for the identical tab_id — both see no existing
        # TabSession yet (chrome.debugger.attach is a real round-trip to the
        # extension, wide enough to race) and both send chrome.debugger.attach,
        # the second of which Chrome rejects with "Another debugger is
        # already attached". One lock per tab_id serializes the two callers
        # so the second sees the first's now-populated TabSession instead.
        self._attach_locks: dict[int, asyncio.Lock] = {}
        self._cdp_generation = 0

    def connect_cdp_client(self, send_to_cdp_client: SendToCDPClient) -> None:
        self._send_to_cdp_client = send_to_cdp_client
        self._cdp_generation += 1

    def _emit(self, message: dict[str, Any]) -> None:
        if self._send_to_cdp_client is not None:
            self._send_to_cdp_client(message)

    # ─── Extension → model inputs ──────────────────────────────────────

    def on_tab_created(self, tab: dict[str, Any]) -> None:
        tab_id = tab.get("id")
        if tab_id is None:
            return
        self._known_tabs[tab_id] = tab
        # Skip tabs chrome.debugger can never attach (chrome://, the Web Store,
        # devtools, …). Attempting it fails mid-flight and used to wedge the whole
        # session — one open chrome://extensions tab was enough to kill it.
        if self._auto_attach and _is_attachable(tab):
            asyncio.create_task(self._attach_tab_safe(tab_id))

    def on_tab_removed(self, tab_id: int) -> None:
        self._known_tabs.pop(tab_id, None)
        self._detach_tab(tab_id)

    def on_debugger_event(self, source: dict[str, Any], method: str, params: Any) -> None:
        tab_id = source.get("tabId")
        if tab_id is None:
            return
        tab_session = self._tab_sessions.get(tab_id)
        if tab_session is None:
            return
        child_session_id = params.get("sessionId") if isinstance(params, dict) else None
        if method == "Target.attachedToTarget" and child_session_id:
            tab_session.child_sessions.add(child_session_id)
        elif method == "Target.detachedFromTarget" and child_session_id:
            tab_session.child_sessions.discard(child_session_id)
        session_id = source.get("sessionId") or tab_session.session_id
        self._emit({"sessionId": session_id, "method": method, "params": params})

    def on_debugger_detach(self, source: dict[str, Any], reason: str | None = None) -> None:
        tab_id = source.get("tabId")
        if tab_id is None:
            return
        self._detach_tab(tab_id)
        # Self-heal a *recoverable* detach: the tab is still open (a same-tab
        # navigation/redirect, or a stray banner event) so re-attach instead of
        # leaving the session dead until a manual reconnect. Skip the cases that
        # can't or shouldn't re-attach: the tab is gone (onTabRemoved handles it),
        # DevTools took the one debugger slot per tab, or the user explicitly
        # cancelled — re-attaching there would just re-prompt them in a loop.
        if (reason not in ("target_closed", "replaced_with_devtools", "canceled_by_user")
                and self._auto_attach and tab_id in self._known_tabs
                and _is_attachable(self._known_tabs.get(tab_id, {}))):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return  # called outside the async handler (sync test) — nothing to schedule
            asyncio.create_task(self._attach_tab_safe(tab_id))

    # ─── Playwright → model commands ───────────────────────────────────

    async def enable_auto_attach(self) -> None:
        self._auto_attach = True
        await asyncio.gather(
            *(self._attach_tab_safe(tab_id) for tab_id in list(self._known_tabs)
              if _is_attachable(self._known_tabs.get(tab_id, {}))),
        )

    async def create_target(self, url: str | None) -> dict[str, Any]:
        # chrome.debugger can't attach chrome://, the Web Store, devtools, … — so
        # refuse before creating an orphan tab we could never drive. Attempting it
        # anyway (create tab, attach throws) used to leave the session stuck on a
        # dead tab.
        if url and not _is_attachable({"url": url}):
            raise RuntimeError(f"cannot drive an un-attachable URL: {url}")
        tab = await self._send_to_extension("chrome.tabs.create", [{"url": url}])
        tab_id = tab.get("id") if tab else None
        if tab_id is None:
            raise RuntimeError("Failed to create tab")
        self._known_tabs[tab_id] = tab
        try:
            tab_session = await self._attach_tab(tab_id)
        except Exception:
            # Attach failed after the tab was created — close the orphan so it
            # can't become a stuck active tab, then surface a clean error.
            self._known_tabs.pop(tab_id, None)
            try:
                await self._send_to_extension("chrome.tabs.remove", [tab_id])
            except Exception:
                pass
            raise
        target_info = tab_session.target_info or {}
        return {"targetId": target_info.get("targetId")}

    async def close_target(self, target_id: str | None) -> dict[str, Any]:
        tab_session = self._find_tab_session(
            lambda s: (s.target_info or {}).get("targetId") == target_id
        ) if target_id else None
        if tab_session is None:
            return {"success": False}
        await self._send_to_extension("chrome.tabs.remove", [tab_session.tab_id])
        return {"success": True}

    def get_target_info(self, session_id: str | None) -> dict[str, Any] | None:
        if not session_id:
            return None
        tab_session = self._find_tab_session(lambda s: s.session_id == session_id)
        return tab_session.target_info if tab_session else None

    def new_client_session(self, tab_session: TabSession) -> str:
        """A genuinely distinct session id for context.new_cdp_session(page)
        -- Target.attachToTarget/attachToBrowserTarget used to just hand
        back the tab's own main session id, which seemed fine (Playwright
        got a working session id either way) until it wasn't: Playwright's
        driver tracks CDP sessions by identity internally, and two of its
        own session objects sharing one server-side id corrupted that
        bookkeeping — confirmed live as a real Node-side assertion crash
        ("Connection closed while reading from the driver") on the very
        next real page action after any raw command went through the
        explicit session. Real CDP genuinely supports multiple simultaneous
        sessions attached to one target; this is that, at last. Reuses the
        same child_sessions routing already built for worker/oopif
        auto-attach (BrowserModel.send_command's fallback), so no new
        dispatch path is needed — a session id here just needs to be found
        in *some* TabSession's child_sessions to route correctly.
        """
        session_id = f"pw-cdp-{self._next_session_id}"
        self._next_session_id += 1
        tab_session.child_sessions.add(session_id)
        return session_id

    def get_tab_id(self, session_id: str | None) -> int | None:
        """The real chrome tab id behind a relay session id — for commands
        that aren't CDP at all (chrome.windows.update, a plain extension API
        unrelated to chrome.debugger) but still need to know *which tab's
        window*. mcp_server.py never sees real chrome tab ids otherwise;
        this is the one seam where it needs to. Checks a tab's own main
        session id and its child sessions (new_client_session()'s explicit
        CDPSessions live there) — same dual lookup send_command already
        does, since callers reach this via either kind of session id."""
        if not session_id:
            return None
        tab_session = self._find_tab_session(lambda s: s.session_id == session_id)
        if tab_session is None:
            tab_session = self._find_tab_session(lambda s: session_id in s.child_sessions)
        return tab_session.tab_id if tab_session else None

    async def send_browser_command(self, method: str, params: Any) -> Any:
        """A browser-level command (Storage.*, Browser.*, ...) with no target
        of its own — chrome.debugger.sendCommand still needs *a* tab, so any
        attached one does, since the result is the same regardless."""
        tab_session = next(iter(self._tab_sessions.values()), None)
        if tab_session is None:
            raise RuntimeError(f"No attached tab to forward browser-level command: {method}")
        return await self._send_to_extension(
            "chrome.debugger.sendCommand", [{"tabId": tab_session.tab_id}, method, params]
        )

    async def send_command(self, session_id: str, method: str, params: Any) -> Any:
        tab_session = self._find_tab_session(lambda s: s.session_id == session_id)
        cdp_session_id = None
        if tab_session is None:
            tab_session = self._find_tab_session(lambda s: session_id in s.child_sessions)
            cdp_session_id = session_id
        if tab_session is None:
            raise RuntimeError(f"No tab found for sessionId: {session_id}")
        target: dict[str, Any] = {"tabId": tab_session.tab_id}
        if cdp_session_id:
            target["sessionId"] = cdp_session_id
        return await self._send_to_extension("chrome.debugger.sendCommand", [target, method, params])

    # ─── Internals ──────────────────────────────────────────────────────

    async def _attach_tab_safe(self, tab_id: int) -> None:
        try:
            await self._attach_tab(tab_id)
        except Exception:
            logger.exception("failed to attach tab %s", tab_id)

    async def _attach_tab(self, tab_id: int) -> TabSession:
        # A tab already tracked here didn't necessarily reach the *current* CDP
        # client: `_tab_sessions` outlives any single CDP connection (only an
        # extension reconnect resets it), but `enable_auto_attach()` re-runs on
        # every fresh `Target.setAutoAttach` — i.e. every new connect_over_cdp.
        # Skipping the emit on a cache hit meant a second connection saw zero
        # attached tabs (`Target.attachedToTarget` only ever fired once, to
        # whichever client was first) even though chrome.debugger was still
        # genuinely attached — real Target semantics replay attachedToTarget
        # for every live target on each new setAutoAttach, so this does too.
        # Gated on announced_generation (see TabSession) so a same-client race
        # on this same tab_id (two callers hitting this cache-hit branch
        # before the winner's own attach even finished) doesn't double-emit
        # to a client that already got the event once.
        lock = self._attach_locks.setdefault(tab_id, asyncio.Lock())
        async with lock:
            existing = self._tab_sessions.get(tab_id)
            if existing is not None:
                if existing.announced_generation != self._cdp_generation:
                    self._emit_attached(existing)
                return existing
            await self._send_to_extension("chrome.debugger.attach", [{"tabId": tab_id}, "1.3"])
            result = await self._send_to_extension(
                "chrome.debugger.sendCommand", [{"tabId": tab_id}, "Target.getTargetInfo"]
            )
            target_info = (result or {}).get("targetInfo")
            session_id = f"pw-tab-{self._next_session_id}"
            self._next_session_id += 1
            tab_session = TabSession(tab_id=tab_id, session_id=session_id, target_info=target_info)
            self._tab_sessions[tab_id] = tab_session
            self._emit_attached(tab_session)
            return tab_session

    def _emit_attached(self, tab_session: TabSession) -> None:
        tab_session.announced_generation = self._cdp_generation
        self._emit({
            "method": "Target.attachedToTarget",
            "params": {
                "sessionId": tab_session.session_id,
                "targetInfo": {**(tab_session.target_info or {}), "attached": True},
                "waitingForDebugger": False,
            },
        })

    def _detach_tab(self, tab_id: int) -> None:
        tab_session = self._tab_sessions.pop(tab_id, None)
        if tab_session is None:
            return
        self._emit({
            "method": "Target.detachedFromTarget",
            "params": {
                "sessionId": tab_session.session_id,
                "targetId": (tab_session.target_info or {}).get("targetId"),
            },
        })

    def _find_tab_session(self, predicate: Callable[[TabSession], bool]) -> TabSession | None:
        for session in self._tab_sessions.values():
            if predicate(session):
                return session
        return None


async def handle_cdp_command(
    model: BrowserModel, method: str, params: Any, session_id: str | None
) -> Any:
    """The dispatcher cdpRelay.ts's _handleCDPCommand + cdpRelayV2.ts's
    handleCDPCommand together form: a couple of commands answered directly,
    a few answered from the model's tab bookkeeping, everything else
    forwarded to the extension."""
    if method == "Browser.getVersion":
        return {
            "protocolVersion": "1.3",
            "product": "Chrome/Extension-Bridge",
            "userAgent": "zerodom-relay/0.1.0",
        }
    if method == "Browser.setDownloadBehavior":
        return {}
    if method == "Target.setAutoAttach":
        if session_id:  # only the root session enables auto-attach
            return None
        await model.enable_auto_attach()
        return {}
    if method == "Target.createTarget":
        return await model.create_target((params or {}).get("url"))
    if method == "Target.closeTarget":
        return await model.close_target((params or {}).get("targetId"))
    if method == "Target.getTargetInfo":
        return model.get_target_info(session_id)
    if method == "Target.attachToTarget":
        # Needed for Playwright's context.new_cdp_session(page) — its only
        # way to reach a CDP method with no high-level wrapper (e.g.
        # Input.setIgnoreInputEvents). Hands back a genuinely distinct
        # session id via new_client_session(), not the tab's own main one —
        # see its docstring for why that distinction turned out to matter.
        target_id = (params or {}).get("targetId")
        tab_session = model._find_tab_session(
            lambda s: (s.target_info or {}).get("targetId") == target_id
        )
        if tab_session is None:
            raise RuntimeError(f"Target.attachToTarget: unknown targetId {target_id!r}")
        return {"sessionId": model.new_client_session(tab_session)}
    if method == "Target.attachToBrowserTarget":
        # context.new_cdp_session()'s actual first move (confirmed live,
        # not documented anywhere obvious) — and one chrome.debugger can
        # never satisfy for real: a tab-scoped chrome.debugger attachment is
        # flatly rejected for a browser-level target ("Not allowed", real
        # Chrome error). Silently broke every raw CDP session ever taken
        # since D15 (Input.setIgnoreInputEvents, the real input-block half
        # of the driving-lock UI) — masked because _set_input_ignored
        # swallows all exceptions by design, so the visible border/cursor
        # kept rendering with no sign real input was never actually
        # blocked. Faked the same way as Target.attachToTarget above: this
        # model has no real distinction between a "browser" session and a
        # "tab" session — every command ends up at
        # chrome.debugger.sendCommand({tabId}, ...) regardless — so any
        # attached tab does, via a genuinely distinct session id from
        # new_client_session() (see its docstring for why that matters).
        tab_session = next(iter(model._tab_sessions.values()), None)
        if tab_session is None:
            raise RuntimeError("Target.attachToBrowserTarget: no attached tab available")
        return {"sessionId": model.new_client_session(tab_session)}
    if method == "Browser.setWindowBounds":
        # A real, recognized CDP Browser-domain method — required, not just
        # convenient: Playwright's own driver validates method names against
        # its internal protocol schema before ever sending them, so a made-up
        # name like "chrome.windows.update" gets rejected client-side with
        # "wasn't found" and never reaches this relay at all (confirmed the
        # hard way). CDP's own Browser.setWindowBounds normally needs a
        # windowId from Browser.getWindowForTarget first; this relay skips
        # that ceremony since it already knows the tab from `session_id` —
        # `windowId` in `params` is accepted but ignored, not real CDP
        # semantics, just reusing a name Playwright won't block. The actual
        # window resize is chrome.windows.update, a plain extension API
        # unrelated to chrome.debugger's Browser domain entirely.
        tab_id = model.get_tab_id(session_id)
        if tab_id is None:
            raise RuntimeError("Browser.setWindowBounds: no tab for this session")
        bounds = (params or {}).get("bounds") or {}
        await model._send_to_extension("chrome.windows.update", [tab_id, bounds])
        # Real CDP's Browser.setWindowBounds response is bare {} -- the
        # extension's chrome.windows.update() actually returns the full
        # chrome.windows.Window object, but forwarding that verbatim
        # crashed Playwright's own Node driver outright (a real internal
        # assertion failure, "Connection closed while reading from the
        # driver", not a graceful Python-side error) — it evidently has
        # internal expectations about this specific method's response
        # shape. Discarding the extension's result and returning the real
        # empty shape is what real CDP does anyway.
        return {}
    if session_id:
        return await model.send_command(session_id, method, params)
    return await model.send_browser_command(method, params)


class RelayServer:
    """One local WS server, two path-routed roles on the same port — matches
    the reference's /cdp/{id} and /extension/{id} split, minus the parts of
    cdpRelay.ts that spawn Chrome and pick a profile: the user's browser and
    the extension are already there, so this only ever bridges, never
    launches.

    Only one extension and one Playwright (our own connect_over_cdp) client
    at a time — matches mcp_server.py's own single-global-session convention
    rather than introducing a different concurrency model for this feature.
    """

    def __init__(self, id: str | None = None):
        self._id = id or uuid.uuid4().hex
        self._cdp_path = f"/cdp/{self._id}"
        self._extension_path = f"/extension/{self._id}"
        self._model: BrowserModel | None = None
        self._extension_ws: Any = None
        self._cdp_ws: Any = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        # Set once the extension has pushed its initial tabs and
        # extension.initialized — see BrowserModel's docstring on why
        # nothing may reach the model before this.
        self._extension_ready = asyncio.Event()
        self._server: Any = None
        self._port: int = 0

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        # websockets' default max_size (1 MiB) is a sane public-internet default,
        # not a sane one here: a real page's serialized HTML (parser.py targets
        # 5,000+ node pages) or a report.py screenshot routinely exceeds it, and
        # a frame over the limit doesn't just error the one message — it kills
        # the whole extension connection (close code 1009), taking every
        # attached tab down with it. This is a local, single-user loopback
        # bridge, not a public endpoint, so there's no DoS threat model to size
        # a limit against.
        self._server = await websockets.serve(self._route, host, port, max_size=None)
        self._port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def cdp_endpoint(self) -> str:
        return f"ws://127.0.0.1:{self._port}{self._cdp_path}"

    def extension_endpoint(self) -> str:
        return f"ws://127.0.0.1:{self._port}{self._extension_path}"

    async def _route(self, ws: Any) -> None:
        # A real vulnerability, not a theoretical one — confirmed live: any
        # public webpage's JS can open a WebSocket straight to this
        # loopback port (WebSocket has no browser-enforced CORS the way
        # fetch() does — a server that doesn't check Origin itself accepts
        # anything) and speak this same JSON-RPC protocol, driving the
        # user's own attached session. Worse since the path stopped being a
        # random per-run UUID (Stage 6b, for reconnect convenience) — a
        # fixed, guessable "/cdp/local" is no obscurity at all. The real
        # fix is Origin, not path secrecy: legitimate callers are either
        # our own extension (a `chrome-extension://` origin) or a
        # non-browser client like Playwright/Python (no Origin header at
        # all) — a page-origin ("http://"/"https://") gets rejected
        # outright, regardless of path.
        origin = ws.request.headers.get("Origin", "")
        # A sandboxed iframe or a file:// page sends `Origin: null` rather than an
        # http(s):// scheme — still a page origin, so reject it too. Legit callers
        # (our extension → chrome-extension://, Playwright/Python → no Origin) are
        # unaffected.
        if origin.startswith("http://") or origin.startswith("https://") or origin == "null":
            await ws.close(1008, "rejected: page origins may not connect to this relay")
            return
        path = ws.request.path
        if path == self._extension_path:
            await self._handle_extension(ws)
        elif path == self._cdp_path:
            await self._handle_cdp(ws)
        else:
            await ws.close(1008, "not found")

    # ─── The extension's own connection ────────────────────────────────

    async def _handle_extension(self, ws: Any) -> None:
        if self._extension_ws is not None:
            await ws.close(1000, "Another extension connection already established")
            return
        self._extension_ws = ws
        self._model = BrowserModel(self._send_to_extension)
        try:
            async for raw in ws:
                self._handle_extension_message(json.loads(raw))
        except websockets.exceptions.ConnectionClosed:
            pass  # a client dropping (even uncleanly, no close frame) is normal,
            # not a handler failure — swallow it so the relay logs stay readable
            # and the finally-cleanup still runs. See _handle_cdp for the same.
        finally:
            self._extension_ws = None
            self._extension_ready.clear()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("extension disconnected"))
            self._pending.clear()

    async def _send_to_extension(self, method: str, params: Any) -> Any:
        if self._extension_ws is None:
            raise RuntimeError("Extension not connected")
        self._next_id += 1
        msg_id = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._extension_ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        return await fut

    def _handle_extension_message(self, msg: dict[str, Any]) -> None:
        msg_id = msg.get("id")
        if msg_id is not None:
            fut = self._pending.pop(msg_id, None)
            if fut is None:
                logger.warning("unexpected response id from extension: %s", msg_id)
            elif not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(str(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))
            return
        method, params = msg.get("method"), msg.get("params") or []
        model = self._model
        if model is None:
            return
        if method == "chrome.debugger.onEvent":
            source, cdp_method, cdp_params = (list(params) + [None, None, None])[:3]
            model.on_debugger_event(source, cdp_method, cdp_params or {})
        elif method == "chrome.debugger.onDetach":
            # background.js sends [source, reason]; the reason decides recovery.
            model.on_debugger_detach(params[0], params[1] if len(params) > 1 else None)
        elif method == "chrome.tabs.onCreated":
            model.on_tab_created(params[0])
        elif method == "chrome.tabs.onRemoved":
            model.on_tab_removed(params[0])
        elif method == "extension.initialized":
            self._extension_ready.set()

    # ─── Our own connect_over_cdp connection ───────────────────────────

    async def _handle_cdp(self, ws: Any) -> None:
        if self._cdp_ws is not None:
            await ws.close(1000, "Another CDP client already connected")
            return
        # Unlike the reference (which only opens this endpoint after its own
        # establishExtensionConnection() already awaited readiness), zerodom's
        # MCP server and this relay are separate processes — a CDP connect
        # attempt can arrive before the extension has opened connect.html at
        # all. Wait here rather than reject, but bound it: an untimed wait meant
        # a first-run user who calls a tool before loading the extension got a
        # ~30s hang (the connect_over_cdp caller's own timeout) and an opaque
        # error. Close cleanly after 10s so the CDP socket isn't held open and
        # the caller fails fast with a legible message.
        try:
            await asyncio.wait_for(self._extension_ready.wait(), timeout=EXTENSION_READY_TIMEOUT)
        except asyncio.TimeoutError:
            await ws.close(1013, "ZeroDOM extension not connected — load it (zerodom extension) and wait for the cyan tab group")
            return
        self._cdp_ws = ws
        assert self._model is not None  # guaranteed once _extension_ready is set

        # A model-emitted event (e.g. Target.attachedToTarget, fired from deep
        # inside handling some other command) and that other command's own
        # JSON-RPC response must reach the client in the order they were
        # produced. The reference gets this for free — Node's ws.send() is a
        # synchronous enqueue, so a plain function call can't reorder two
        # sends relative to each other. asyncio has no such guarantee:
        # `asyncio.create_task(ws.send(...))` merely schedules a send, and two
        # scheduled sends can complete in either order. One writer draining
        # one queue is what actually preserves call order here.
        out_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def _writer() -> None:
            while True:
                message = await out_queue.get()
                await ws.send(json.dumps(message))

        def send_to_cdp_client(message: dict[str, Any]) -> None:
            out_queue.put_nowait(message)

        self._model.connect_cdp_client(send_to_cdp_client)
        writer_task = asyncio.create_task(_writer())
        try:
            async for raw in ws:
                asyncio.create_task(
                    self._handle_playwright_message(send_to_cdp_client, json.loads(raw))
                )
        except websockets.exceptions.ConnectionClosed:
            pass  # Playwright/driver disconnecting (incl. an unclean drop with no
            # close frame) is a normal end-of-session, not a crash — swallow it so
            # the connection handler doesn't log a traceback and wedge the log.
        finally:
            self._cdp_ws = None
            writer_task.cancel()

    async def _handle_playwright_message(
        self, send_to_cdp_client: SendToCDPClient, msg: dict[str, Any]
    ) -> None:
        msg_id, session_id, method, params = msg.get("id"), msg.get("sessionId"), msg.get("method"), msg.get("params")
        assert self._model is not None
        try:
            result = await handle_cdp_command(self._model, method, params, session_id)
            send_to_cdp_client({"id": msg_id, "sessionId": session_id, "result": result})
        except Exception as exc:
            send_to_cdp_client({"id": msg_id, "sessionId": session_id, "error": {"message": str(exc)}})


# How long a CDP client waits for the extension to attach before the relay closes
# the socket cleanly (rather than the old untimed wait, which hung a first-run
# caller ~30s). Module-level so tests can shrink it.
EXTENSION_READY_TIMEOUT = 10

DEFAULT_PORT = 8765  # fixed, not ephemeral: a human re-pastes the printed URL into
# the extension popup and an env var by hand, potentially minutes apart and across
# `zerodom relay` restarts — a random port every time turns one-time setup into a
# recurring chore. Tests still get isolated random ports via RelayServer()'s own
# default (port=0 in start()); only the CLI entrypoint fixes it.


async def run_relay(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    """`zerodom relay` — the whole point of Stage 3, runnable by a human.
    Prints what to open and what to export, then serves until interrupted."""
    server = RelayServer(id="local")
    await server.start(host, port)
    # flush=True: stdout is fully buffered whenever it's not a TTY (piped to
    # a log file, captured by a process manager, ...) — without it these
    # instructions could sit invisible in the buffer for as long as the
    # server runs, which is indefinitely.
    print("zerodom relay listening.\n", flush=True)
    print("1. Install the zerodom extension (one-time): load extension/ unpacked", flush=True)
    print("   via chrome://extensions -> Developer mode -> Load unpacked.\n", flush=True)
    print("2. Click the zerodom extension icon and paste this URL, then Connect:", flush=True)
    print(f"   {server.extension_endpoint()}\n", flush=True)
    print("3. In another terminal:", flush=True)
    print(f"   export ZERODOM_CDP_ENDPOINT={server.cdp_endpoint()}", flush=True)
    print("   uvx --from zerodom zerodom-mcp   # or run any zerodom tool as usual\n", flush=True)
    print("Ctrl+C to stop.", flush=True)
    try:
        await asyncio.Event().wait()  # serve until interrupted
    finally:
        await server.stop()


def main() -> None:
    try:
        asyncio.run(run_relay())
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        # EADDRINUSE is the common one: a relay (or the MCP server's auto-spawned
        # relay) is already on this port. One clean line beats an asyncio traceback.
        if exc.errno == errno.EADDRINUSE:
            raise SystemExit(
                f"zerodom: port {DEFAULT_PORT} is already in use — a zerodom relay is "
                f"probably already running (that's usually fine; stop it with Ctrl+C in "
                f"its terminal, or `pkill -f 'zerodom relay'`, if you need to restart it)."
            )
        raise


if __name__ == "__main__":
    main()
