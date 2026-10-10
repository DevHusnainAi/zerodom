"""Dynamic-component correctness suite — HYPOTHESIS-ACK-PLAN §3.2 (T3.3).

    uv run pytest tests/test_dynamic_components.py

Drives the real MCP session (parse -> find -> click/fill, no fakes) against the
zerodom-demo fixture app on a local port, so CI proves the three failure shapes
never produce a *wrong action*, with no external network:

  /virtual    React virtualized list that reorders (churn) or recreates its DOM
              between read and click — the node zerodom named must be the node
              that gets clicked (T3.1's pre-action identity check).
  /shadow     custom element with an open shadow root: a light-DOM decoy and a
              shadow control carry the same label; each click/fill must reach
              the tree the node came from.
  /hydration  SSR page whose client handler lands late — a click while
              hydration is pending must be flagged, not silently lost.

Skipped unless Chromium is installed (same gate as test_playwright.py).
"""
from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from zerodom import mcp_server

pytestmark = pytest.mark.skipif(
    not list(Path.home().glob(".cache/ms-playwright/chromium*")),
    reason="chromium not installed (run: uv run playwright install chromium)",
)

# The fixture app is FastAPI on uvicorn (dev deps). Without them the fixture
# would error at setup rather than skip — so skip the file cleanly, the same way
# the chromium gate above does, when a partial env hasn't installed the dev group.
pytest.importorskip("fastapi", reason="demo fixture server needs fastapi (dev dep)")
pytest.importorskip("uvicorn", reason="demo fixture server needs uvicorn (dev dep)")

ROOT = Path(__file__).resolve().parents[1]


# --- the fixture app, on a free port, one uvicorn thread for the module ------

@pytest.fixture(scope="module")
def demo_server():
    spec = spec_from_file_location(
        "zerodom_demo_server", ROOT / "zerodom-demo" / "server.py"
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(
        uvicorn.Config(module.app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("zerodom-demo fixture server never came up")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


# --- one real MCP session per test, on a worker thread's own event loop ------

def _reset_session() -> None:
    """Fresh `_session` — same shape test_mcp_cdp.clean_session uses, plus the
    keys `_read` writes (hydration/blocked/cleared) so state can't leak."""
    mcp_server._session.update(
        pw=None, browser=None, pages={}, active=None, _next_tab=0, attached=False,
        selectors={}, nodes=[], url=None, frames=False, viewport_only=False,
        check_occlusion=False, hidden_fields=[], network_log={}, cdp_sessions={},
        action_count=0, aria_cache_url=None, aria_cache_tokens=None,
        navigated_at=0.0, identities={}, scope=None, clearance=None,
        clearance_note=None, hydration_pending=False, blocked=None, cleared=None,
    )


async def _in_session(scenario):
    """Run one scenario inside a single event loop: the async Playwright driver
    started by _page() must live and die on the same loop, so setup, tools and
    teardown all happen inside this one coroutine."""
    _reset_session()
    try:
        return await scenario()
    finally:
        try:
            await mcp_server._teardown_active_session()
        finally:
            _reset_session()


def run_mcp(scenario, timeout: float = 120):
    # Worker thread: other test modules may have installed Playwright's greenlet
    # loop on the main one (the test_playwright.py precedent), and asyncio.run
    # cannot share a loop that's already there.
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(lambda: asyncio.run(_in_session(scenario))).result(timeout)


def _node_id(line: str) -> str:
    m = re.search(r"\[([^\]]+)\]", line)
    assert m, f"no node id in {line!r}"
    return m.group(1)


async def _find_id(query: str) -> str:
    line = await mcp_server.zerodom_find(query)
    return _node_id(line)


def _patch_env(monkeypatch) -> None:
    # A configured-but-unreachable relay endpoint would make _page() attach
    # instead of launching; these tests own their own browser.
    monkeypatch.delenv("ZERODOM_CDP_ENDPOINT", raising=False)
    monkeypatch.delenv("ZERODOM_SCOPE", raising=False)
    monkeypatch.setenv("ZERODOM_NO_SOLVER", "1")


# --- /virtual: re-render churn ----------------------------------------------

def test_recreated_nodes_still_click_the_named_order(demo_server, monkeypatch):
    """Same rows, brand-new DOM nodes (React key change): the node zerodom
    named at read time is the element that gets clicked."""
    _patch_env(monkeypatch)
    base = demo_server

    async def scenario():
        await mcp_server.zerodom_parse_url(base + "/virtual")
        node_id = await _find_id("ORD-0007")
        await mcp_server.zerodom_eval_js("window.recreate()")
        out = await mcp_server.zerodom_click_node(node_id)
        assert out.startswith(f"clicked [{node_id}]")
        return json.loads(await mcp_server.zerodom_eval_js("window.hits"))

    assert run_mcp(scenario) == ["ORD-0007"]


def test_reordered_rows_never_mis_click(demo_server, monkeypatch):
    """The virtualized window slides: every row shifts position between read
    and click. The spec's assertion is exact — the click must land on the order
    the agent named (ORD-0004) or be refused outright, never on a neighbour."""
    _patch_env(monkeypatch)
    base = demo_server

    async def scenario():
        await mcp_server.zerodom_parse_url(base + "/virtual")
        node_id = await _find_id("ORD-0004")
        await mcp_server.zerodom_eval_js("window.churn()")
        refused = None
        try:
            await mcp_server.zerodom_click_node(node_id)
        except ValueError as exc:
            refused = str(exc)
        hits = json.loads(await mcp_server.zerodom_eval_js("window.hits"))
        if refused is not None:
            assert "page changed under us" in refused, refused
            assert hits == [], f"refused but still acted: {hits}"
        else:
            assert hits == ["ORD-0004"], (
                f"clicked the wrong row under churn: {hits} "
                "(named ORD-0004, got a neighbour)"
            )

    run_mcp(scenario)


# --- /shadow: open-root resolve ---------------------------------------------

def test_shadow_controls_resolve_and_click_their_own_tree(demo_server, monkeypatch):
    """Light decoy and shadow control share the label "Confirm order". Each
    node must click the tree it came from — a crossed selector shows up as the
    same hit twice (or the wrong one), never as two distinct hits."""
    _patch_env(monkeypatch)
    base = demo_server

    async def scenario():
        await mcp_server.zerodom_parse_url(base + "/shadow")
        await mcp_server.zerodom_click_node(await _find_id("Apply discount"))
        confirm_lines = (await mcp_server.zerodom_find("Confirm order")).splitlines()
        assert len(confirm_lines) == 2, confirm_lines
        for line in confirm_lines:
            await mcp_server.zerodom_click_node(_node_id(line))
        return json.loads(await mcp_server.zerodom_eval_js("window.hits"))

    hits = run_mcp(scenario)
    assert sorted(hits) == ["light-confirm", "shadow-confirm", "shadow-discount"], hits


def test_fill_reaches_the_input_inside_the_shadow_root(demo_server, monkeypatch):
    """Reading an open root is only half the job — the fill has to land in it."""
    _patch_env(monkeypatch)
    base = demo_server

    async def scenario():
        await mcp_server.zerodom_parse_url(base + "/shadow")
        node_id = await _find_id("PO number")
        await mcp_server.zerodom_fill_node(node_id, "PO-77")
        return json.loads(await mcp_server.zerodom_eval_js(
            "document.getElementById('ordw')"
            ".shadowRoot.querySelector('input[name=po]').value"
        ))

    assert run_mcp(scenario) == "PO-77"


# --- /hydration: pending flag ------------------------------------------------

def test_click_during_pending_hydration_is_flagged_not_lost(demo_server, monkeypatch):
    """SSR button, client handler lands 8s late. The click inside the grace
    window is reported as a probable hydration miss — and really was a miss
    (zero hits) — so retrying after mount lands the action instead of the
    agent trusting a click that did nothing."""
    _patch_env(monkeypatch)
    base = demo_server

    async def scenario():
        await mcp_server.zerodom_parse_url(base + "/hydration")
        assert mcp_server._session["hydration_pending"] is True
        node_id = await _find_id("Complete purchase")

        flagged_out = await mcp_server.zerodom_click_node(node_id)
        hits_after_flag = json.loads(await mcp_server.zerodom_eval_js("window.hits"))
        assert "may not have finished attaching" in flagged_out, flagged_out
        assert hits_after_flag == [], "pre-mount click should be silently lost"

        await mcp_server.zerodom_eval_js("window.__mount()")
        await mcp_server.zerodom_read_page()
        assert mcp_server._session["hydration_pending"] is False

        retry_out = await mcp_server.zerodom_click_node(node_id)
        hits_after_retry = json.loads(await mcp_server.zerodom_eval_js("window.hits"))
        return flagged_out, hits_after_flag, retry_out, hits_after_retry

    flagged_out, hits1, retry_out, hits2 = run_mcp(scenario)
    assert hits1 == []
    assert hits2 == ["ssr-action"], "after mount the same node id must click for real"
    assert "may not have finished attaching" not in retry_out, (
        "the flag must clear once the framework has mounted"
    )
