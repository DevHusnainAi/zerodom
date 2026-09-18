import asyncio
from typing import Any

import pytest

from zerodom import mcp_server


class FakeGotoPage:
    """A page with just enough surface for _page()/_restart() to run: .goto()."""

    def __init__(self):
        self.url = "https://example.com/"

    async def goto(self, url, **kw):
        self.url = url


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
        pw=None, browser=None, page=None, attached=False,
        selectors={}, nodes=[], url=None,
    )
    yield
    mcp_server._session.update(
        pw=None, browser=None, page=None, attached=False,
        selectors={}, nodes=[], url=None,
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
        pw=fake_pw, browser=browser, page=SpyPage(), attached=True,
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
        pw=fake_pw, browser=browser, page=None, attached=False,
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

    async def evaluate(self, js):
        self.evaluate_calls.append(js)

    async def click(self, *a, **kw):
        self.click_calls += 1

    async def fill(self, text, *a, **kw):
        self.fill_calls.append(text)


def _setup_single_node_session(attached: bool) -> TrackingLocator:
    node = {"id": "node_01", "type": "button", "label": "Go", "selector": "#go"}
    mcp_server._session.update(
        page=object(), attached=attached, selectors={}, nodes=[node], url=None,
    )
    return TrackingLocator()


def test_highlight_fires_before_acting_on_an_attached_session(monkeypatch, clean_session):
    locator = _setup_single_node_session(attached=True)
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)
    monkeypatch.setattr(mcp_server, "_HIGHLIGHT_PAUSE_S", 0)  # don't actually sleep in tests

    asyncio.run(mcp_server._act("01", "click"))

    assert locator.evaluate_calls, "attached session should inject the highlight before acting"
    assert locator.click_calls == 1


def test_highlight_is_skipped_for_a_launched_headless_session(monkeypatch, clean_session):
    locator = _setup_single_node_session(attached=False)
    monkeypatch.setattr(mcp_server, "locate", lambda page, node: locator)

    asyncio.run(mcp_server._act("01", "click"))

    assert locator.evaluate_calls == [], "a headless, unwatched session has nobody to show the pulse to"
    assert locator.click_calls == 1


def test_a_highlight_failure_never_blocks_the_real_action(monkeypatch, clean_session):
    class BrokenHighlightLocator(TrackingLocator):
        async def evaluate(self, js):
            raise RuntimeError("element detached mid-evaluate")

    locator = BrokenHighlightLocator()
    mcp_server._session.update(
        page=object(), attached=True, selectors={},
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


def _setup_logging_session(attached: bool) -> tuple[TrackingLocator, TrackingPage]:
    node = {"id": "node_01", "type": "button", "label": "Go", "selector": "#go"}
    page = TrackingPage()
    mcp_server._session.update(
        page=page, attached=attached, selectors={}, nodes=[node], url=None,
    )
    return TrackingLocator(), page


def test_action_gets_logged_to_the_on_page_panel_when_attached(monkeypatch, clean_session):
    locator, page = _setup_logging_session(attached=True)
    monkeypatch.setattr(mcp_server, "locate", lambda p, node: locator)
    monkeypatch.setattr(mcp_server, "_HIGHLIGHT_PAUSE_S", 0)

    asyncio.run(mcp_server._act("01", "click"))

    assert len(page.evaluate_calls) == 1
    js, text = page.evaluate_calls[0]
    assert js == mcp_server._LOG_JS
    assert "clicked" in text and "Go" in text


def test_fill_is_logged_with_the_typed_value(monkeypatch, clean_session):
    locator, page = _setup_logging_session(attached=True)
    monkeypatch.setattr(mcp_server, "locate", lambda p, node: locator)
    monkeypatch.setattr(mcp_server, "_HIGHLIGHT_PAUSE_S", 0)

    asyncio.run(mcp_server._act("01", "fill", "husnain@example.com"))

    _, text = page.evaluate_calls[0]
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
    mcp_server._session.update(page=BrokenLogPage())
    monkeypatch.setattr(mcp_server, "locate", lambda p, node: locator)
    monkeypatch.setattr(mcp_server, "_HIGHLIGHT_PAUSE_S", 0)

    asyncio.run(mcp_server._act("01", "click"))  # must not raise

    assert locator.click_calls == 1
