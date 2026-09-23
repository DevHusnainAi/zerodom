import asyncio
import json
import os as _os
from typing import Any

import pytest

from zerodom import mcp_server


class FakeGotoPage:
    """A page with just enough surface for _page()/_restart() to run: .goto()."""

    def __init__(self, networkidle_raises=False):
        self.url = "https://example.com/"
        self.networkidle_raises = networkidle_raises
        self.load_state_calls: list[tuple] = []

    async def goto(self, url, **kw):
        self.url = url

    async def wait_for_load_state(self, state=None, timeout=None):
        self.load_state_calls.append((state, timeout))
        if state == "networkidle" and self.networkidle_raises:
            raise TimeoutError("networkidle wait timed out")

    async def close(self):
        pass

    def on(self, event, handler):
        pass


class FakeBrowser:
    def __init__(self, pages):
        self.contexts = [FakeContext(pages)]
        self.closed = False

    async def close(self):
        self.closed = True

    async def new_page(self):
        page = FakeGotoPage()
        self.contexts[0].pages.append(page)
        return page


class FakeContext:
    def __init__(self, pages):
        self.pages = pages

    async def new_page(self):
        page = FakeGotoPage()
        self.pages.append(page)
        return page


class FakePw:
    """Stands in for playwright.async_api.async_playwright()'s return value."""

    def __init__(self):
        self.chromium = FakeChromium()
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakeChromium:
    def __init__(self):
        self.launch_called = False
        self.connect_called_with = None

    async def launch(self):
        self.launch_called = True
        return FakeBrowser(pages=[])

    async def connect_over_cdp(self, endpoint):
        self.connect_called_with = endpoint
        return FakeBrowser(pages=[FakeGotoPage()])  # one already-open tab


@pytest.fixture
def clean_session():
    mcp_server._session.update(
        pw=None, browser=None, pages={}, active=None, _next_tab=0, attached=False,
        selectors={}, nodes=[], url=None, network_log={}, cdp_sessions={},
    )
    yield
    mcp_server._session.update(
        pw=None, browser=None, pages={}, active=None, _next_tab=0, attached=False,
        selectors={}, nodes=[], url=None, network_log={}, cdp_sessions={},
    )


def _patch_async_playwright(monkeypatch, fake_pw):
    class FakeAsyncPlaywrightContext:
        async def start(self):
            return fake_pw

    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: FakeAsyncPlaywrightContext()
    )


def test_page_launches_a_fresh_browser_with_no_endpoint(monkeypatch, clean_session):
    monkeypatch.delenv("ZERODOM_CDP_ENDPOINT", raising=False)
    fake_pw = FakePw()
    _patch_async_playwright(monkeypatch, fake_pw)

    asyncio.run(mcp_server._page())

    assert fake_pw.chromium.launch_called
    assert fake_pw.chromium.connect_called_with is None
    assert mcp_server._session["attached"] is False


def test_page_attaches_over_cdp_when_endpoint_is_set(monkeypatch, clean_session):
    monkeypatch.setenv("ZERODOM_CDP_ENDPOINT", "http://127.0.0.1:9222")
    fake_pw = FakePw()
    _patch_async_playwright(monkeypatch, fake_pw)

    asyncio.run(mcp_server._page())

    assert not fake_pw.chromium.launch_called
    assert fake_pw.chromium.connect_called_with == "http://127.0.0.1:9222"
    assert mcp_server._session["attached"] is True


def test_restart_does_not_close_an_attached_browser(monkeypatch, clean_session):
    """The regression this exists to catch: closing a CDP-attached browser on
    crash-recovery would take down the user's real Chrome, not just detach."""

    class SpyBrowser:
        def __init__(self):
            self.close_called = False

        async def close(self):
            self.close_called = True

    class SpyPage:
        url = "https://example.com/"

    browser = SpyBrowser()
    fake_pw = FakePw()
    mcp_server._session.update(
        pw=fake_pw, browser=browser, pages={"0": SpyPage()}, active="0", attached=True,
        selectors={}, nodes=[], url="https://example.com/",
    )
    monkeypatch.setenv("ZERODOM_CDP_ENDPOINT", "http://127.0.0.1:9222")
    _patch_async_playwright(monkeypatch, FakePw())

    async def fake_read(*a, **kw):
        return ""

    monkeypatch.setattr(mcp_server, "_read", fake_read)

    asyncio.run(mcp_server._restart("https://example.com/"))

    assert not browser.close_called, "attached browser must be disconnected, never closed"
    assert fake_pw.stopped, "the old pw handle is still ours to stop"


def test_restart_closes_a_launched_browser(monkeypatch, clean_session):
    """The non-regression: a launched (not attached) browser is ours, so a crash
    still tears it down as before."""

    class SpyBrowser:
        def __init__(self):
            self.close_called = False

        async def close(self):
            self.close_called = True

    browser = SpyBrowser()
    fake_pw = FakePw()
    mcp_server._session.update(
        pw=fake_pw, browser=browser, attached=False,
        selectors={}, nodes=[], url="https://example.com/",
    )
    monkeypatch.delenv("ZERODOM_CDP_ENDPOINT", raising=False)
    _patch_async_playwright(monkeypatch, FakePw())

    async def fake_read(*a, **kw):
        return ""

    monkeypatch.setattr(mcp_server, "_read", fake_read)

    asyncio.run(mcp_server._restart("https://example.com/"))

    assert browser.close_called, "a launched browser is ours to close on crash-recovery"


class TrackingLocator:
    """Stands in for whatever locate() returns — tracks .evaluate() (the
    highlight injection) and the actual action call separately, so a test
    can tell whether the highlight fired without caring how the real click
    or fill happens underneath it."""

    def __init__(self):
        self.evaluate_calls: list[str] = []
        self.click_calls = 0
        self.fill_calls: list[str] = []
        self.hover_calls = 0
        self.press_calls: list[str] = []
        self.upload_calls: list[str] = []
        self.drag_to_calls: list[Any] = []
        self.press_sequentially_calls: list[str] = []

    async def evaluate(self, js, arg=None):
        self.evaluate_calls.append(js)

    async def click(self, *a, **kw):
        self.click_calls += 1

    async def fill(self, text, *a, **kw):
        self.fill_calls.append(text)

    async def press_sequentially(self, text, *a, **kw):
        self.press_sequentially_calls.append(text)

    async def hover(self, *a, **kw):
        self.hover_calls += 1

    async def press(self, key, *a, **kw):
        self.press_calls.append(key)

    async def set_input_files(self, path, *a, **kw):
        self.upload_calls.append(path)

    async def drag_to(self, target, *a, **kw):
        self.drag_to_calls.append(target)


def _setup_single_node_session(attached: bool) -> TrackingLocator:
    node = {"id": "node_01", "type": "button", "label": "Go", "selector": "#go"}
    mcp_server._session.update(
        pages={"0": object()}, active="0", attached=attached, selectors={}, nodes=[node], url=None,
    )
    return TrackingLocator()


def _setup_two_node_session(attached: bool):
    source_node = {"id": "node_01", "type": "div", "label": "Item", "selector": "#item"}
    target_node = {"id": "node_02", "type": "div", "label": "Drop zone", "selector": "#drop"}
    mcp_server._session.update(
        pages={"0": object()}, active="0", attached=attached, selectors={},
        nodes=[source_node, target_node], url=None,
    )
    source_locator, target_locator = TrackingLocator(), TrackingLocator()

    def fake_locate(page, node):
        return source_locator if node["id"] == "node_01" else target_locator

    return source_locator, target_locator, fake_locate


def test_highlight_fires_before_acting_on_an_attached_session(monkeypatch, clean_session):
    locator = _setup_single_node_session(attached=True)
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)
    monkeypatch.setattr(mcp_server, "_HIGHLIGHT_PAUSE_S", 0)  # don't actually sleep in tests

    asyncio.run(mcp_server._act("01", "click"))

    assert locator.evaluate_calls, "attached session should inject the highlight before acting"
    assert locator.click_calls == 1


def test_fill_on_a_content_editable_node_types_instead_of_calling_fill(monkeypatch, clean_session):
    node = {
        "id": "node_01", "type": "div", "label": "Message", "selector": "#composer",
        "content_editable": True,
    }
    mcp_server._session.update(
        pages={"0": object()}, active="0", attached=False, selectors={}, nodes=[node], url=None,
    )
    locator = TrackingLocator()
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)

    asyncio.run(mcp_server._act("01", "fill", "hello"))

    assert locator.click_calls == 1, "a real click establishes the caret these editors listen for"
    assert locator.press_sequentially_calls == ["hello"]
    assert locator.fill_calls == []


def test_fill_on_a_normal_input_still_calls_fill(monkeypatch, clean_session):
    locator = _setup_single_node_session(attached=False)
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)

    asyncio.run(mcp_server._act("01", "fill", "hello"))

    assert locator.fill_calls == ["hello"]
    assert locator.press_sequentially_calls == []


def test_highlight_is_skipped_for_a_launched_headless_session(monkeypatch, clean_session):
    locator = _setup_single_node_session(attached=False)
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)

    asyncio.run(mcp_server._act("01", "click"))

    assert locator.evaluate_calls == [], "a headless, unwatched session has nobody to show the pulse to"
    assert locator.click_calls == 1


def _sensitive_field_session(attached: bool, **node_extra) -> TrackingLocator:
    node = {"id": "node_01", "type": "input", "label": "Password", "selector": "#pw", **node_extra}
    mcp_server._session.update(
        pages={"0": object()}, active="0", attached=attached, selectors={}, nodes=[node], url=None,
    )
    return TrackingLocator()


def test_fill_is_refused_on_a_password_input_type_when_attached(monkeypatch, clean_session):
    locator = _sensitive_field_session(attached=True, input_type="password", label="Enter your secret")
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)

    try:
        asyncio.run(mcp_server._act("01", "fill", "hunter2"))
        assert False, "expected PermissionError"
    except PermissionError:
        pass
    assert locator.fill_calls == [], "must never dispatch the fill, not even try and roll back"


def test_fill_is_refused_on_a_text_field_labeled_cvv_when_attached(monkeypatch, clean_session):
    locator = _sensitive_field_session(attached=True, input_type="text", label="CVV")
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)

    try:
        asyncio.run(mcp_server._act("01", "fill", "123"))
        assert False, "expected PermissionError"
    except PermissionError:
        pass
    assert locator.fill_calls == []


def test_fill_on_a_password_field_is_allowed_for_a_launched_headless_session(monkeypatch, clean_session):
    # The guard is specifically about a real, watched session filling a real
    # person's credentials -- a throwaway headless browser has no such risk.
    locator = _sensitive_field_session(attached=False, input_type="password")
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)

    asyncio.run(mcp_server._act("01", "fill", "hunter2"))

    assert locator.fill_calls == ["hunter2"]


def test_fill_on_an_ordinary_field_is_unaffected_when_attached(monkeypatch, clean_session):
    locator = _sensitive_field_session(attached=True, input_type="email", label="Email address")
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)

    asyncio.run(mcp_server._act("01", "fill", "a@b.com"))

    assert locator.fill_calls == ["a@b.com"]


def test_a_highlight_failure_never_blocks_the_real_action(monkeypatch, clean_session):
    class BrokenHighlightLocator(TrackingLocator):
        async def evaluate(self, js):
            raise RuntimeError("element detached mid-evaluate")

    locator = BrokenHighlightLocator()
    mcp_server._session.update(
        pages={"0": object()}, active="0", attached=True, selectors={},
        nodes=[{"id": "node_01", "type": "button", "label": "Go", "selector": "#go"}],
        url=None,
    )
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)
    monkeypatch.setattr(mcp_server, "_HIGHLIGHT_PAUSE_S", 0)

    asyncio.run(mcp_server._act("01", "click"))  # must not raise

    assert locator.click_calls == 1


class TrackingPage:
    """Tracks page.evaluate() calls — what _log_action() uses, separate from
    the locator's own .evaluate() that _highlight() uses."""

    def __init__(self):
        self.evaluate_calls: list[tuple[str, Any]] = []

    async def evaluate(self, js, arg=None):
        self.evaluate_calls.append((js, arg))


class FakeCDPSession:
    """Stands in for what page.context.new_cdp_session(page) returns —
    tracks raw CDP sends, what _set_input_ignored uses to reach
    Input.setIgnoreInputEvents (no Playwright high-level wrapper exists)."""

    def __init__(self):
        self.sent: list[tuple[str, Any]] = []

    async def send(self, method, params=None):
        self.sent.append((method, params))


class FakeCDPContext:
    def __init__(self, session):
        self.session = session

    async def new_cdp_session(self, page):
        return self.session


def _setup_logging_session(attached: bool) -> tuple[TrackingLocator, TrackingPage]:
    node = {"id": "node_01", "type": "button", "label": "Go", "selector": "#go"}
    page = TrackingPage()
    mcp_server._session.update(
        pages={"0": page}, active="0", attached=attached, selectors={}, nodes=[node], url=None,
    )
    return TrackingLocator(), page


def test_action_gets_logged_to_the_on_page_panel_when_attached(monkeypatch, clean_session):
    locator, page = _setup_logging_session(attached=True)
    monkeypatch.setattr(mcp_server, "locate", lambda p, node: locator)
    monkeypatch.setattr(mcp_server, "_HIGHLIGHT_PAUSE_S", 0)

    asyncio.run(mcp_server._act("01", "click"))

    log_calls = [(js, arg) for js, arg in page.evaluate_calls if js == mcp_server._LOG_JS]
    assert len(log_calls) == 1
    _, text = log_calls[0]
    assert "clicked" in text and "Go" in text


def test_fill_is_logged_with_the_typed_value(monkeypatch, clean_session):
    locator, page = _setup_logging_session(attached=True)
    monkeypatch.setattr(mcp_server, "locate", lambda p, node: locator)
    monkeypatch.setattr(mcp_server, "_HIGHLIGHT_PAUSE_S", 0)

    asyncio.run(mcp_server._act("01", "fill", "husnain@example.com"))

    _, text = next((js, arg) for js, arg in page.evaluate_calls if js == mcp_server._LOG_JS)
    assert "filled" in text and "husnain@example.com" in text


def test_nothing_is_logged_for_a_launched_headless_session(monkeypatch, clean_session):
    locator, page = _setup_logging_session(attached=False)
    monkeypatch.setattr(mcp_server, "locate", lambda p, node: locator)

    asyncio.run(mcp_server._act("01", "click"))

    assert page.evaluate_calls == []


def test_a_logging_failure_never_blocks_the_real_action(monkeypatch, clean_session):
    class BrokenLogPage(TrackingPage):
        async def evaluate(self, js, arg=None):
            raise RuntimeError("page navigated away mid-evaluate")

    locator, _ = _setup_logging_session(attached=True)
    mcp_server._session["pages"]["0"] = BrokenLogPage()
    monkeypatch.setattr(mcp_server, "locate", lambda p, node: locator)
    monkeypatch.setattr(mcp_server, "_HIGHLIGHT_PAUSE_S", 0)

    asyncio.run(mcp_server._act("01", "click"))  # must not raise

    assert locator.click_calls == 1


def test_ensure_relay_is_a_noop_when_endpoint_already_set(monkeypatch):
    monkeypatch.setenv("ZERODOM_CDP_ENDPOINT", "ws://127.0.0.1:9999/cdp/x")
    monkeypatch.setattr(mcp_server.subprocess, "Popen", lambda *a, **kw: pytest.fail("should not spawn"))

    mcp_server._ensure_relay_running()

    assert mcp_server._cdp_endpoint() == "ws://127.0.0.1:9999/cdp/x"


def test_ensure_relay_reuses_an_already_listening_relay(monkeypatch):
    monkeypatch.delenv("ZERODOM_CDP_ENDPOINT", raising=False)
    monkeypatch.setattr(mcp_server, "_relay_port_open", lambda host, port: True)
    monkeypatch.setattr(mcp_server.subprocess, "Popen", lambda *a, **kw: pytest.fail("should not spawn"))

    mcp_server._ensure_relay_running()

    assert mcp_server._cdp_endpoint() == "ws://127.0.0.1:8765/cdp/local"


def test_ensure_relay_spawns_one_when_nothing_is_listening(monkeypatch, tmp_path):
    monkeypatch.delenv("ZERODOM_CDP_ENDPOINT", raising=False)
    monkeypatch.setattr(mcp_server, "_RELAY_LOG_PATH", tmp_path / "relay.log")
    state = {"open": False, "spawned": 0}

    def fake_popen(*args, **kwargs):
        state["spawned"] += 1
        state["open"] = True  # pretend the relay just bound the port

        class FakeProcess:
            pass

        return FakeProcess()

    monkeypatch.setattr(mcp_server, "_relay_port_open", lambda host, port: state["open"])
    monkeypatch.setattr(mcp_server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mcp_server.time, "sleep", lambda s: None)

    mcp_server._ensure_relay_running()

    assert state["spawned"] == 1
    assert mcp_server._cdp_endpoint() == "ws://127.0.0.1:8765/cdp/local"


def _bootstrap_headless(monkeypatch):
    """No CDP endpoint -> _page() launches a fresh FakeBrowser, giving tab "0"."""
    monkeypatch.delenv("ZERODOM_CDP_ENDPOINT", raising=False)
    _patch_async_playwright(monkeypatch, FakePw())

    async def fake_read(*a, **kw):
        return "graph"

    monkeypatch.setattr(mcp_server, "_read", fake_read)


def test_new_tab_opens_a_second_tab_and_makes_it_active(monkeypatch, clean_session):
    _bootstrap_headless(monkeypatch)
    asyncio.run(mcp_server._page())
    assert mcp_server._session["active"] == "0"

    result = asyncio.run(mcp_server.zerodom_new_tab("https://second.example/"))

    assert mcp_server._session["active"] == "1"
    assert set(mcp_server._session["pages"]) == {"0", "1"}
    assert "[tab 1]" in result


def test_list_tabs_marks_the_active_one(monkeypatch, clean_session):
    _bootstrap_headless(monkeypatch)
    asyncio.run(mcp_server._page())
    asyncio.run(mcp_server.zerodom_new_tab())

    result = asyncio.run(mcp_server.zerodom_list_tabs())

    lines = result.splitlines()
    assert any(line.startswith("  [tab 0]") for line in lines)
    assert any(line.startswith("* [tab 1]") for line in lines)


def test_switch_tab_changes_which_tab_other_tools_act_on(monkeypatch, clean_session):
    _bootstrap_headless(monkeypatch)
    asyncio.run(mcp_server._page())
    asyncio.run(mcp_server.zerodom_new_tab())
    assert mcp_server._session["active"] == "1"

    asyncio.run(mcp_server.zerodom_switch_tab("0"))

    assert mcp_server._session["active"] == "0"
    assert asyncio.run(mcp_server._page()) is mcp_server._session["pages"]["0"]


def test_switch_tab_rejects_an_unknown_id(monkeypatch, clean_session):
    _bootstrap_headless(monkeypatch)
    asyncio.run(mcp_server._page())

    with pytest.raises(ValueError):
        asyncio.run(mcp_server.zerodom_switch_tab("99"))


def test_close_tab_falls_back_to_a_remaining_tab(monkeypatch, clean_session):
    _bootstrap_headless(monkeypatch)
    asyncio.run(mcp_server._page())
    asyncio.run(mcp_server.zerodom_new_tab())
    assert mcp_server._session["active"] == "1"

    asyncio.run(mcp_server.zerodom_close_tab("1"))

    assert "1" not in mcp_server._session["pages"]
    assert mcp_server._session["active"] == "0"


def test_close_tab_refuses_to_close_the_last_tab(monkeypatch, clean_session):
    _bootstrap_headless(monkeypatch)
    asyncio.run(mcp_server._page())

    with pytest.raises(ValueError):
        asyncio.run(mcp_server.zerodom_close_tab("0"))


class FakeToolsPage:
    """Enough surface for screenshot/viewport/cookies/eval — deliberately
    separate from FakeGotoPage since those tools need a richer fake
    (.context.cookies(), .evaluate(), .screenshot()) than the bootstrap-path
    tests do."""

    def __init__(self):
        self.url = "https://example.com/"
        self.screenshot_calls: list[tuple] = []
        self.viewport_calls: list[dict] = []
        self.eval_calls: list[str] = []
        self.context = _FakeCookieContext()

    async def screenshot(self, path=None, full_page=False):
        self.screenshot_calls.append((path, full_page))

    async def set_viewport_size(self, size):
        self.viewport_calls.append(size)

    async def evaluate(self, code):
        self.eval_calls.append(code)
        return {"echo": code}


class _FakeCookieContext:
    async def cookies(self):
        return [{
            "name": "session", "value": "abc123", "domain": "example.com",
            "httpOnly": True, "secure": True, "sameSite": "Strict",
        }]


def test_screenshot_saves_to_a_generated_path_by_default(clean_session):
    page = FakeToolsPage()
    mcp_server._session.update(pages={"0": page}, active="0")

    result = asyncio.run(mcp_server.zerodom_screenshot())

    assert len(page.screenshot_calls) == 1
    path, full_page = page.screenshot_calls[0]
    assert full_page is True
    assert path in result
    assert _os.path.exists(path)
    _os.remove(path)


def test_screenshot_uses_an_explicit_path(clean_session, tmp_path):
    page = FakeToolsPage()
    mcp_server._session.update(pages={"0": page}, active="0")
    target = str(tmp_path / "out.png")

    result = asyncio.run(mcp_server.zerodom_screenshot(target))

    assert page.screenshot_calls == [(target, True)]
    assert target in result


def test_set_viewport_resizes_the_page_and_rereads(monkeypatch, clean_session):
    page = FakeToolsPage()
    mcp_server._session.update(pages={"0": page}, active="0")

    async def fake_read(*a, **kw):
        return "graph"

    monkeypatch.setattr(mcp_server, "_read", fake_read)

    asyncio.run(mcp_server.zerodom_set_viewport(375, 812))

    assert page.viewport_calls == [{"width": 375, "height": 812}]


def test_get_cookies_reports_the_httponly_and_samesite_flags(clean_session):
    page = FakeToolsPage()
    mcp_server._session.update(pages={"0": page}, active="0")

    result = asyncio.run(mcp_server.zerodom_get_cookies())

    assert "session=abc123" in result
    assert "httpOnly=True" in result
    assert "sameSite=Strict" in result


def test_eval_js_runs_in_the_page_and_returns_json(clean_session):
    page = FakeToolsPage()
    mcp_server._session.update(pages={"0": page}, active="0")

    result = asyncio.run(mcp_server.zerodom_eval_js("1+1"))

    assert page.eval_calls == ["1+1"]
    assert json.loads(result) == {"echo": "1+1"}


class FakeEventPage:
    """Captures page.on() handlers so a test can fire them directly, the way
    real request/response events would arrive."""

    def __init__(self):
        self.url = "https://example.com/"
        self.handlers: dict[str, Any] = {}

    def on(self, event, handler):
        self.handlers[event] = handler


class FakeRequest:
    def __init__(self, method, url):
        self.method, self.url = method, url


class FakeResponse:
    def __init__(self, status, url):
        self.status, self.url = status, url


def test_wire_network_log_captures_requests_and_responses(clean_session):
    page = FakeEventPage()
    mcp_server._wire_network_log(page, "0")

    page.handlers["request"](FakeRequest("GET", "https://example.com/"))
    page.handlers["response"](FakeResponse(200, "https://example.com/"))

    log = mcp_server._session["network_log"]["0"]
    assert log[0] == {"type": "request", "method": "GET", "url": "https://example.com/"}
    assert log[1] == {"type": "response", "status": 200, "url": "https://example.com/"}


def test_wire_network_log_caps_at_200_entries(clean_session):
    page = FakeEventPage()
    mcp_server._wire_network_log(page, "0")

    for i in range(250):
        page.handlers["request"](FakeRequest("GET", f"https://example.com/{i}"))

    log = mcp_server._session["network_log"]["0"]
    assert len(log) == 200
    assert log[-1]["url"] == "https://example.com/249"  # newest kept
    assert log[0]["url"] == "https://example.com/50"  # oldest 50 dropped


def test_network_log_tool_reports_and_clears(clean_session):
    page = FakeEventPage()
    mcp_server._session.update(pages={"0": page}, active="0")
    mcp_server._wire_network_log(page, "0")
    page.handlers["request"](FakeRequest("GET", "https://example.com/"))

    result = asyncio.run(mcp_server.zerodom_network_log())
    assert result.split()[1:] == ["GET", "https://example.com/"]

    asyncio.run(mcp_server.zerodom_network_log(clear=True))
    assert mcp_server._session["network_log"]["0"] == []


def test_network_log_reports_when_nothing_captured_yet(clean_session):
    page = FakeEventPage()
    mcp_server._session.update(pages={"0": page}, active="0")
    mcp_server._wire_network_log(page, "0")

    result = asyncio.run(mcp_server.zerodom_network_log())

    assert result == "No requests captured yet."


def test_zerodom_hover_tool_hovers_and_returns_formatted_result(monkeypatch, clean_session):
    locator = _setup_single_node_session(attached=True)
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)
    monkeypatch.setattr(mcp_server, "_HIGHLIGHT_PAUSE_S", 0)
    monkeypatch.setattr(mcp_server, "_read", lambda *a, **kw: _async_str("graph"))

    result = asyncio.run(mcp_server.zerodom_hover("01"))

    assert locator.hover_calls == 1
    assert result.startswith("hovered [01]")


def test_zerodom_press_key_tool_sends_the_key(monkeypatch, clean_session):
    locator = _setup_single_node_session(attached=True)
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)
    monkeypatch.setattr(mcp_server, "_HIGHLIGHT_PAUSE_S", 0)
    monkeypatch.setattr(mcp_server, "_read", lambda *a, **kw: _async_str("graph"))

    result = asyncio.run(mcp_server.zerodom_press_key("01", "Enter"))

    assert locator.press_calls == ["Enter"]
    assert result.startswith("pressed 'Enter' on [01]")


def test_zerodom_upload_file_tool_sets_input_files(monkeypatch, clean_session):
    locator = _setup_single_node_session(attached=True)
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)
    monkeypatch.setattr(mcp_server, "_HIGHLIGHT_PAUSE_S", 0)
    monkeypatch.setattr(mcp_server, "_read", lambda *a, **kw: _async_str("graph"))

    result = asyncio.run(mcp_server.zerodom_upload_file("01", "/tmp/resume.pdf"))

    assert locator.upload_calls == ["/tmp/resume.pdf"]
    assert result.startswith("uploaded '/tmp/resume.pdf' to [01]")


def test_zerodom_drag_tool_drags_source_onto_target_and_only_highlights_source(
    monkeypatch, clean_session
):
    source_locator, target_locator, fake_locate = _setup_two_node_session(attached=True)
    monkeypatch.setattr(mcp_server, "locate", fake_locate)
    monkeypatch.setattr(mcp_server, "_HIGHLIGHT_PAUSE_S", 0)
    monkeypatch.setattr(mcp_server, "_read", lambda *a, **kw: _async_str("graph"))

    result = asyncio.run(mcp_server.zerodom_drag("01", "02"))

    assert source_locator.drag_to_calls == [target_locator]
    assert source_locator.evaluate_calls, "source should be highlighted before dragging"
    assert target_locator.evaluate_calls == [], "only the source gets the highlight pulse"
    assert result.startswith("dragged [01] -> [02]")


async def _async_str(value: str) -> str:
    return value


def test_act_locks_real_input_except_for_its_own_action(monkeypatch, clean_session):
    """The actual behavior the browser-level block requires to not break
    zerodom's own clicks: Input.setIgnoreInputEvents(true) before acting (and
    while idle), (false) for the split second the real action runs, (true)
    again immediately after — sent over the cached CDP session, not the DOM."""
    node = {"id": "node_01", "type": "button", "label": "Go", "selector": "#go"}
    cdp_session = FakeCDPSession()
    page = TrackingPage()
    page.context = FakeCDPContext(cdp_session)
    locator = TrackingLocator()
    locator.page = page
    mcp_server._session.update(
        pages={"0": page}, active="0", attached=True, selectors={}, nodes=[node], url=None,
        cdp_sessions={},
    )
    monkeypatch.setattr(mcp_server, "locate", lambda p, node: locator)
    monkeypatch.setattr(mcp_server, "_HIGHLIGHT_PAUSE_S", 0)

    asyncio.run(mcp_server._act("01", "click"))

    ignore_calls = [
        params["ignore"] for method, params in cdp_session.sent
        if method == "Input.setIgnoreInputEvents"
    ]
    assert ignore_calls == [True, False, True]
    assert locator.click_calls == 1
    assert mcp_server._MOVE_CURSOR_JS in locator.evaluate_calls
    # The DOM overlay is cosmetic only now — no page.evaluate_calls entry
    # should ever reference the lock; that name doesn't exist anymore.
    assert not hasattr(mcp_server, "_SET_LOCK_JS")


def test_act_is_not_locked_out_by_its_own_lock_layer_on_a_headless_session(
    monkeypatch, clean_session
):
    """A launched (unwatched) session has no lock layer at all — nothing to
    toggle, and _unlocked_input must be a true no-op there, not an error."""
    locator = _setup_single_node_session(attached=False)
    monkeypatch.setattr(mcp_server, "locate", lambda p, node: locator)

    asyncio.run(mcp_server._act("01", "click"))  # must not raise

    assert locator.click_calls == 1


class FakeStylesLocator:
    def __init__(self, result):
        self.result = result
        self.eval_calls: list[str] = []

    async def evaluate(self, js):
        self.eval_calls.append(js)
        return self.result


def test_get_styles_returns_curated_computed_styles(monkeypatch, clean_session):
    node = {"id": "node_01", "type": "button", "label": "Go", "selector": "#go"}
    mcp_server._session.update(
        pages={"0": object()}, active="0", attached=False, selectors={}, nodes=[node], url=None,
    )
    fake_result = {
        "box": {"x": 1, "y": 2, "width": 3, "height": 4},
        "styles": {"color": "rgb(0, 0, 0)", "display": "flex"},
    }
    locator = FakeStylesLocator(fake_result)
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)

    result = asyncio.run(mcp_server.zerodom_get_styles("01"))

    assert locator.eval_calls == [mcp_server._COMPUTED_STYLE_JS]
    assert json.loads(result) == fake_result


def test_goto_gives_a_bounded_top_up_for_hydration_after_domcontentloaded():
    """The google.com/maps finding: domcontentloaded alone undercounted a
    heavy client-hydrated SPA's real controls (10 nodes vs. 39 once given
    time to mount). _goto() should wait for domcontentloaded first, then
    give networkidle a bounded, best-effort top-up."""
    page = FakeGotoPage()

    asyncio.run(mcp_server._goto(page, "https://example.com"))

    assert page.url == "https://example.com"
    networkidle_calls = [c for c in page.load_state_calls if c[0] == "networkidle"]
    assert len(networkidle_calls) == 1
    assert networkidle_calls[0][1] == 2000  # bounded, not indefinite


def test_goto_proceeds_when_networkidle_never_settles():
    """A page with a persistent websocket/poll (chat, live dashboards) never
    goes idle — this must not hang the whole call waiting for it."""
    page = FakeGotoPage(networkidle_raises=True)

    asyncio.run(mcp_server._goto(page, "https://example.com"))  # must not raise

    assert page.url == "https://example.com"
