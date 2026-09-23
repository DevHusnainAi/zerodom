"""Real WebSocket round-trips through RelayServer — no fake objects standing
in for the network this time, unlike test_relay.py's BrowserModel tests.
Both "sides" here are genuine websockets clients; only the browser and the
real Chrome extension are missing, which is the one thing this project
correctly can't automate (ROADMAP.md's "one real, manual check").

Every test runs its whole start-server -> exercise -> stop-server sequence
inside one asyncio.run() call. A websockets Server is bound to the event
loop it was created in — starting it in one asyncio.run() and trying to use
it from another (as a fixture spanning multiple asyncio.run() calls would)
hangs every connection attempt, since the loop that was accepting
connections is already closed by the time the next asyncio.run() starts a
new one.
"""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
import websockets

from zerodom.relay import RelayServer


@asynccontextmanager
async def running_server():
    server = RelayServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def run(coro):
    return asyncio.run(coro)


async def _recv_json(ws):
    return json.loads(await ws.recv())


def test_extension_connects_and_pushes_the_initial_handshake():
    async def scenario():
        async with running_server() as server:
            async with websockets.connect(server.extension_endpoint()) as ext:
                await ext.send(json.dumps({"method": "chrome.tabs.onCreated", "params": [{"id": 1, "url": "https://example.com/"}]}))
                await ext.send(json.dumps({"method": "extension.initialized", "params": []}))
                await asyncio.sleep(0.05)  # let the server-side handler process both
                assert server._extension_ready.is_set()
                assert 1 in server._model._known_tabs

    run(scenario())


def test_cdp_client_waits_for_extension_before_getting_a_response():
    """The actual adaptation from the reference this test exists to prove:
    a CDP connection made before the extension has shown up doesn't get
    rejected, it waits — see the comment in RelayServer._handle_cdp."""

    async def scenario():
        async with running_server() as server:
            async with websockets.connect(server.cdp_endpoint()) as cdp:
                await cdp.send(json.dumps({"id": 1, "method": "Browser.getVersion", "params": {}}))

                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(cdp.recv(), timeout=0.2)

                async with websockets.connect(server.extension_endpoint()) as ext:
                    await ext.send(json.dumps({"method": "extension.initialized", "params": []}))
                    response = await asyncio.wait_for(_recv_json(cdp), timeout=2)
                    assert response["result"]["product"] == "Chrome/Extension-Bridge"

    run(scenario())


def test_full_attach_and_command_round_trip():
    """The real thing Stage 3 exists to prove: a command issued from the
    "Playwright" side reaches the "extension" side and a result comes back,
    through the actual network stack."""

    async def extension_side(server, ready: asyncio.Event, done: asyncio.Event):
        async with websockets.connect(server.extension_endpoint()) as ext:
            await ext.send(json.dumps({"method": "chrome.tabs.onCreated", "params": [{"id": 7, "url": "https://example.com/"}]}))
            await ext.send(json.dumps({"method": "extension.initialized", "params": []}))
            ready.set()

            async for raw in ext:
                msg = json.loads(raw)
                method, params = msg["method"], msg["params"]
                if method == "chrome.debugger.attach":
                    await ext.send(json.dumps({"id": msg["id"], "result": None}))
                elif method == "chrome.debugger.sendCommand":
                    target, cdp_method, *_ = params
                    if cdp_method == "Target.getTargetInfo":
                        result = {"targetInfo": {"targetId": "target-7", "url": "https://example.com/"}}
                    elif cdp_method == "Runtime.evaluate":
                        result = {"result": {"value": "hello from the real tab"}}
                    else:
                        result = {}
                    await ext.send(json.dumps({"id": msg["id"], "result": result}))

    async def cdp_side(server, ready: asyncio.Event, done: asyncio.Event):
        await ready.wait()
        async with websockets.connect(server.cdp_endpoint()) as cdp:
            await cdp.send(json.dumps({"id": 1, "sessionId": None, "method": "Target.setAutoAttach", "params": {}}))

            # Two messages come back for command id=1: the unsolicited
            # Target.attachedToTarget event (fired from inside
            # enable_auto_attach, mid-command) and then the command's own
            # {"id": 1, "result": {}} response — in that order, which is
            # exactly the ordering test_relay.py's queue exists to guarantee.
            attached = await asyncio.wait_for(_recv_json(cdp), timeout=2)
            assert attached["method"] == "Target.attachedToTarget"
            session_id = attached["params"]["sessionId"]
            assert session_id == "pw-tab-1"

            auto_attach_response = await asyncio.wait_for(_recv_json(cdp), timeout=2)
            assert auto_attach_response == {"id": 1, "sessionId": None, "result": {}}

            await cdp.send(json.dumps({
                "id": 2, "sessionId": session_id, "method": "Runtime.evaluate",
                "params": {"expression": "document.title"},
            }))
            response = await asyncio.wait_for(_recv_json(cdp), timeout=2)
            assert response["result"]["result"]["value"] == "hello from the real tab"
            done.set()

    async def scenario():
        async with running_server() as server:
            ready, done = asyncio.Event(), asyncio.Event()
            ext_task = asyncio.create_task(extension_side(server, ready, done))
            await asyncio.wait_for(cdp_side(server, ready, done), timeout=5)
            # cdp_side finishing doesn't make extension_side's `async for`
            # return on its own — it's parked waiting for a next message
            # that's never coming. Cancel it rather than require a
            # cooperative exit from inside a blocking receive loop.
            ext_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await ext_task

    run(scenario())


def test_a_second_extension_connection_is_refused():
    async def scenario():
        async with running_server() as server:
            async with websockets.connect(server.extension_endpoint()):
                async with websockets.connect(server.extension_endpoint()) as second:
                    with pytest.raises(websockets.ConnectionClosed):
                        await second.recv()

    run(scenario())


def test_an_unknown_path_is_refused():
    # The upgrade itself succeeds (routing happens after accept, not via
    # process_request) — the server closes right after, so the client sees
    # a normal ConnectionClosed on its first read, not a rejected handshake.
    # Fine for a single-user local tool whose real paths are secret UUIDs
    # nobody will guess; a process_request-level reject is more precise but
    # not worth the extra code for what this stage actually needs.
    async def scenario():
        async with running_server() as server:
            async with websockets.connect(f"ws://127.0.0.1:{server._port}/not-a-real-path") as ws:
                with pytest.raises(websockets.ConnectionClosed):
                    await ws.recv()

    run(scenario())
