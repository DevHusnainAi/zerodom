"""Real relay + real unpacked extension + real Chromium, end to end.

Deliberately **not** part of the default `uv run pytest` sweep, and
deliberately not wired into CI — see tests/test_relay_server.py's own
docstring: loading a real unpacked extension is "the one thing this project
correctly can't automate" reliably in a CI sandbox (different launch path
entirely — extensions only load via launch_persistent_context() with
--load-extension, not the chromium.launch() path every other test uses; CI
runners add timing/permission variance for chrome.debugger and service-worker
startup that has nothing to do with an actual code regression). Fast,
deterministic regression coverage for the bug classes this exists to catch
lives in tests/test_relay.py instead (BrowserModel-level, controlled-delay
races, zero real browser).

Run manually before a release, or after touching relay.py/background.js.
Stop any already-running `zerodom relay`/`zerodom-mcp` first — the extension
only ever connects to the fixed default port (background.js has no UI left
to point it anywhere else, by design, see D-whatever the friction-removal
commit is), so this test binds the relay there too, and two relays can't
share one port:

    ZERODOM_E2E=1 uv run pytest tests/test_extension_e2e.py -v -s

A real (visible) Chrome window briefly opens — extensions require either
headed mode or Chrome's newer "headless=new", and this only needs to run on
a human's machine occasionally, not silently in CI, so headed is simplest.
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from zerodom.relay import DEFAULT_PORT, RelayServer

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("ZERODOM_E2E") != "1",
        reason="opt-in only: set ZERODOM_E2E=1 to run against a real extension + browser",
    ),
    pytest.mark.skipif(
        not list(Path.home().glob(".cache/ms-playwright/chromium*")),
        reason="chromium not installed (run: uv run playwright install chromium)",
    ),
]

EXTENSION_PATH = Path(__file__).parent.parent / "extension"


@pytest.fixture
def real_relay_and_extension(monkeypatch):
    """Starts a real RelayServer on the extension's fixed default port,
    launches a real Chromium with the real unpacked extension loaded, waits
    for it to connect, and points ZERODOM_CDP_ENDPOINT at it. Tests then use
    zerodom.mcp_server directly, the same way a real MCP client would.
    """

    async def setup():
        server = RelayServer(id="local")
        await server.start(port=DEFAULT_PORT)
        pw = await async_playwright().start()
        user_data_dir = tempfile.mkdtemp(prefix="zerodom-e2e-")
        context = await pw.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            args=[
                f"--disable-extensions-except={EXTENSION_PATH}",
                f"--load-extension={EXTENSION_PATH}",
            ],
        )
        for _ in range(75):  # ~15s for the extension's service worker to connect
            if server._extension_ready.is_set():
                break
            await asyncio.sleep(0.2)
        else:
            await context.close()
            await pw.stop()
            await server.stop()
            raise TimeoutError("extension never connected to the relay")
        return server, pw, context, user_data_dir

    server, pw, context, user_data_dir = asyncio.run(setup())
    monkeypatch.setenv("ZERODOM_CDP_ENDPOINT", server.cdp_endpoint())

    try:
        yield server
    finally:
        async def teardown():
            await context.close()
            await pw.stop()
            await server.stop()

        asyncio.run(teardown())
        shutil.rmtree(user_data_dir, ignore_errors=True)


def test_real_extension_survives_opening_a_second_tab(real_relay_and_extension):
    """The exact real-world path that exposed both bugs fixed this session:
    a fresh attach followed immediately by zerodom_new_tab(), which races
    create_target()'s own attach against the extension's real
    chrome.tabs.onCreated event — something no synchronous fake can open.
    """
    from zerodom import mcp_server

    mcp_server._session.update(
        pw=None, browser=None, pages={}, active=None, _next_tab=0,
        attached=False, selectors={}, nodes=[], url=None, network_log={},
    )

    async def scenario():
        await mcp_server.zerodom_parse_url("https://example.com")
        await mcp_server.zerodom_new_tab("https://www.iana.org/help/example-domains")
        return await mcp_server.zerodom_list_tabs()

    result = asyncio.run(scenario())

    assert "[tab 0]" in result
    assert "[tab 1]" in result
