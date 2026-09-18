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
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import websockets

logger = logging.getLogger("zerodom.relay")

SendToExtension = Callable[[str, Any], Awaitable[Any]]
SendToCDPClient = Callable[[dict[str, Any]], None]


@dataclass
class TabSession:
    tab_id: int
    session_id: str  # "pw-tab-N", assigned by us, never by the extension
    target_info: dict[str, Any] | None
    # Child CDP sessions (workers, oopifs) belonging to this tab, tracked via
    # Target.attachedToTarget / Target.detachedFromTarget events on it.
    child_sessions: set[str] = field(default_factory=set)


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

    def connect_cdp_client(self, send_to_cdp_client: SendToCDPClient) -> None:
        self._send_to_cdp_client = send_to_cdp_client

    def _emit(self, message: dict[str, Any]) -> None:
        if self._send_to_cdp_client is not None:
            self._send_to_cdp_client(message)

    # ─── Extension → model inputs ──────────────────────────────────────

    def on_tab_created(self, tab: dict[str, Any]) -> None:
        tab_id = tab.get("id")
        if tab_id is None:
            return
        self._known_tabs[tab_id] = tab
        if self._auto_attach:
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

    def on_debugger_detach(self, source: dict[str, Any]) -> None:
        tab_id = source.get("tabId")
        if tab_id is not None:
            self._detach_tab(tab_id)

    # ─── Playwright → model commands ───────────────────────────────────

    async def enable_auto_attach(self) -> None:
        self._auto_attach = True
        await asyncio.gather(
            *(self._attach_tab_safe(tab_id) for tab_id in list(self._known_tabs)),
        )

    async def create_target(self, url: str | None) -> dict[str, Any]:
        tab = await self._send_to_extension("chrome.tabs.create", [{"url": url}])
        tab_id = tab.get("id") if tab else None
        if tab_id is None:
            raise RuntimeError("Failed to create tab")
        self._known_tabs[tab_id] = tab
        tab_session = await self._attach_tab(tab_id)
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
        existing = self._tab_sessions.get(tab_id)
        if existing is not None:
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
            model.on_debugger_detach(params[0])
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
        # all. Wait here rather than reject, bounded by whatever timeout the
        # connect_over_cdp caller itself enforces.
        await self._extension_ready.wait()
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


if __name__ == "__main__":
    main()
