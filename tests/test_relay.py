import asyncio

import pytest

from zerodom.relay import BrowserModel, handle_cdp_command


class FakeExtension:
    """Records every call and answers chrome.debugger.attach/sendCommand
    exactly the way a real extension would for a single-tab scenario —
    no real Chrome, no real WebSocket, matching the FakePage/FakePw
    pattern in the other test files."""

    def __init__(self):
        self.calls: list[tuple[str, list]] = []
        self.next_created_tab_id = 100

    async def send(self, method: str, params):
        self.calls.append((method, params))
        if method == "chrome.debugger.attach":
            return None
        if method == "chrome.debugger.sendCommand":
            target, cdp_method, *_ = params
            if cdp_method == "Target.getTargetInfo":
                return {"targetInfo": {"targetId": f"target-{target['tabId']}", "url": "https://example.com/"}}
            return {"ok": True, "method": cdp_method}
        if method == "chrome.tabs.create":
            tab = {"id": self.next_created_tab_id, "url": params[0].get("url")}
            self.next_created_tab_id += 1
            return tab
        if method == "chrome.tabs.remove":
            return None
        raise AssertionError(f"unexpected extension call: {method}")


class FakeCDPClient:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, message):
        self.sent.append(message)


@pytest.fixture
def rig():
    ext = FakeExtension()
    cdp = FakeCDPClient()
    model = BrowserModel(ext.send)
    model.connect_cdp_client(cdp.send)
    return model, ext, cdp


def test_tab_created_before_auto_attach_is_just_recorded(rig):
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://example.com/"})
    assert ext.calls == []
    assert cdp.sent == []


def test_enable_auto_attach_attaches_every_known_tab(rig):
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://a.example/"})
    model.on_tab_created({"id": 2, "url": "https://b.example/"})

    asyncio.run(model.enable_auto_attach())

    attach_calls = [c for c in ext.calls if c[0] == "chrome.debugger.attach"]
    assert {c[1][0]["tabId"] for c in attach_calls} == {1, 2}

    attached_events = [m for m in cdp.sent if m["method"] == "Target.attachedToTarget"]
    assert len(attached_events) == 2
    session_ids = {e["params"]["sessionId"] for e in attached_events}
    assert session_ids == {"pw-tab-1", "pw-tab-2"}
    assert all(e["params"]["targetInfo"]["attached"] is True for e in attached_events)


def test_a_second_auto_attach_reannounces_already_attached_tabs(rig):
    """A tab attached for a first CDP client must be replayed to a *second*
    one too — the tab session is cached on the model (survives a CDP client
    reconnect), but `Target.attachedToTarget` had only ever fired once, so a
    second connect_over_cdp saw zero tabs despite chrome.debugger genuinely
    being attached. Real CDP semantics replay attachedToTarget per client."""
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://example.com/"})
    asyncio.run(model.enable_auto_attach())
    attach_calls_before = len([c for c in ext.calls if c[0] == "chrome.debugger.attach"])

    cdp2 = FakeCDPClient()
    model.connect_cdp_client(cdp2.send)
    asyncio.run(model.enable_auto_attach())

    attach_calls_after = len([c for c in ext.calls if c[0] == "chrome.debugger.attach"])
    assert attach_calls_after == attach_calls_before  # no re-attach, just a cache hit
    attached_events = [m for m in cdp2.sent if m["method"] == "Target.attachedToTarget"]
    assert len(attached_events) == 1
    assert attached_events[0]["params"]["sessionId"] == "pw-tab-1"


def test_a_tab_created_after_auto_attach_is_attached_immediately(rig):
    model, ext, cdp = rig

    async def scenario():
        await model.enable_auto_attach()  # no tabs known yet
        model.on_tab_created({"id": 5, "url": "https://late.example/"})
        await asyncio.sleep(0)  # let the fire-and-forget attach task run

    asyncio.run(scenario())

    assert any(c[0] == "chrome.debugger.attach" and c[1][0]["tabId"] == 5 for c in ext.calls)
    assert any(m["method"] == "Target.attachedToTarget" for m in cdp.sent)


def test_removing_a_tab_emits_detached_from_target(rig):
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://example.com/"})
    asyncio.run(model.enable_auto_attach())
    cdp.sent.clear()

    model.on_tab_removed(1)

    assert cdp.sent == [{
        "method": "Target.detachedFromTarget",
        "params": {"sessionId": "pw-tab-1", "targetId": "target-1"},
    }]


def test_removing_an_unattached_tab_is_a_silent_no_op(rig):
    model, ext, cdp = rig
    model.on_tab_removed(999)  # never attached
    assert cdp.sent == []


def test_debugger_detach_from_the_extension_side_also_emits(rig):
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://example.com/"})
    asyncio.run(model.enable_auto_attach())
    cdp.sent.clear()

    model.on_debugger_detach({"tabId": 1})

    assert cdp.sent[0]["method"] == "Target.detachedFromTarget"


def test_debugger_event_is_relayed_with_the_tabs_session_id(rig):
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://example.com/"})
    asyncio.run(model.enable_auto_attach())
    cdp.sent.clear()

    model.on_debugger_event({"tabId": 1}, "Page.frameNavigated", {"frame": {}})

    assert cdp.sent == [{
        "sessionId": "pw-tab-1", "method": "Page.frameNavigated", "params": {"frame": {}},
    }]


def test_debugger_event_for_an_unattached_tab_is_dropped(rig):
    model, ext, cdp = rig
    model.on_debugger_event({"tabId": 999}, "Page.frameNavigated", {})
    assert cdp.sent == []


def test_child_session_tracking_via_attached_to_target(rig):
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://example.com/"})
    asyncio.run(model.enable_auto_attach())

    model.on_debugger_event(
        {"tabId": 1}, "Target.attachedToTarget", {"sessionId": "worker-session-1"}
    )
    assert "worker-session-1" in model._tab_sessions[1].child_sessions

    model.on_debugger_event(
        {"tabId": 1}, "Target.detachedFromTarget", {"sessionId": "worker-session-1"}
    )
    assert "worker-session-1" not in model._tab_sessions[1].child_sessions


def test_create_target_creates_and_attaches_a_new_tab(rig):
    model, ext, cdp = rig
    result = asyncio.run(model.create_target("https://new.example/"))
    assert result["targetId"] == "target-100"
    assert any(c[0] == "chrome.tabs.create" for c in ext.calls)


def test_create_target_does_not_double_attach_a_racing_tab_created_event():
    # Real Chrome fires chrome.tabs.onCreated for a tab create_target() just
    # made itself, independently of create_target()'s own _attach_tab() call
    # — a genuine race the fixture's synchronous FakeExtension can't expose,
    # so this test wires in an artificial delay on chrome.debugger.attach to
    # open the same window a real async extension round-trip does.
    ext = FakeExtension()
    cdp = FakeCDPClient()
    attach_started = asyncio.Event()
    real_send = ext.send

    async def send_with_delay(method, params):
        if method == "chrome.debugger.attach":
            attach_started.set()
            await asyncio.sleep(0.01)
        return await real_send(method, params)

    model = BrowserModel(send_with_delay)
    model.connect_cdp_client(cdp.send)

    async def run():
        await model.enable_auto_attach()  # no tabs known yet; sets _auto_attach = True
        create_task = asyncio.create_task(model.create_target("https://new.example/"))
        await attach_started.wait()
        model.on_tab_created({"id": 100, "url": "https://new.example/"})  # races the attach above
        await asyncio.sleep(0.05)  # let that racing auto-attach task run to completion too
        await create_task

    asyncio.run(run())

    attach_calls = [c for c in ext.calls if c[0] == "chrome.debugger.attach"]
    assert len(attach_calls) == 1
    # The bug that actually crashed Playwright's driver against a real
    # extension: even once chrome.debugger.attach itself was de-duplicated,
    # the losing racer's cache-hit fallback still re-emitted
    # Target.attachedToTarget for a targetId the client already had —
    # "Duplicate target", fatal to Playwright's Node-side connection.
    attached_events = [m for m in cdp.sent if m["method"] == "Target.attachedToTarget"]
    assert len(attached_events) == 1


def test_close_target_removes_the_matching_tab(rig):
    model, ext, cdp = rig
    asyncio.run(model.create_target("https://new.example/"))
    result = asyncio.run(model.close_target("target-100"))
    assert result == {"success": True}
    assert any(c[0] == "chrome.tabs.remove" and c[1][0] == 100 for c in ext.calls)


def test_close_target_reports_failure_for_an_unknown_target(rig):
    model, ext, cdp = rig
    result = asyncio.run(model.close_target("no-such-target"))
    assert result == {"success": False}


def test_get_target_info_by_session_id(rig):
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://example.com/"})
    asyncio.run(model.enable_auto_attach())
    info = model.get_target_info("pw-tab-1")
    assert info["targetId"] == "target-1"
    assert model.get_target_info(None) is None
    assert model.get_target_info("no-such-session") is None


def test_send_browser_command_routes_through_any_attached_tab(rig):
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://example.com/"})
    asyncio.run(model.enable_auto_attach())

    asyncio.run(model.send_browser_command("Storage.clearDataForOrigin", {}))

    assert any(
        c[0] == "chrome.debugger.sendCommand" and c[1][1] == "Storage.clearDataForOrigin"
        for c in ext.calls
    )


def test_send_browser_command_with_nothing_attached_raises(rig):
    model, ext, cdp = rig
    with pytest.raises(RuntimeError, match="No attached tab"):
        asyncio.run(model.send_browser_command("Storage.clearDataForOrigin", {}))


def test_send_command_routes_by_relay_session_id(rig):
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://example.com/"})
    asyncio.run(model.enable_auto_attach())

    asyncio.run(model.send_command("pw-tab-1", "Runtime.evaluate", {"expression": "1"}))

    call = [c for c in ext.calls if c[0] == "chrome.debugger.sendCommand"][-1]
    target, method, params = call[1]
    assert target == {"tabId": 1}
    assert method == "Runtime.evaluate"


def test_send_command_routes_child_sessions_to_their_owning_tab(rig):
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://example.com/"})
    asyncio.run(model.enable_auto_attach())
    model.on_debugger_event({"tabId": 1}, "Target.attachedToTarget", {"sessionId": "worker-1"})

    asyncio.run(model.send_command("worker-1", "Runtime.evaluate", {}))

    call = [c for c in ext.calls if c[0] == "chrome.debugger.sendCommand"][-1]
    target, method, params = call[1]
    assert target == {"tabId": 1, "sessionId": "worker-1"}


def test_send_command_for_an_unknown_session_raises(rig):
    model, ext, cdp = rig
    with pytest.raises(RuntimeError, match="No tab found"):
        asyncio.run(model.send_command("no-such-session", "Runtime.evaluate", {}))


# ─── handle_cdp_command dispatcher ─────────────────────────────────────────


def test_browser_get_version_is_answered_directly(rig):
    model, _, _ = rig
    result = asyncio.run(handle_cdp_command(model, "Browser.getVersion", {}, None))
    assert result["product"] == "Chrome/Extension-Bridge"


def test_browser_set_download_behavior_is_a_no_op(rig):
    model, _, _ = rig
    assert asyncio.run(handle_cdp_command(model, "Browser.setDownloadBehavior", {}, None)) == {}


def test_set_auto_attach_on_the_root_session_enables_it(rig):
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://example.com/"})
    result = asyncio.run(handle_cdp_command(model, "Target.setAutoAttach", {}, None))
    assert result == {}
    assert model._auto_attach is True


def test_set_auto_attach_on_a_child_session_is_ignored(rig):
    model, _, _ = rig
    result = asyncio.run(handle_cdp_command(model, "Target.setAutoAttach", {}, "pw-tab-1"))
    assert result is None
    assert model._auto_attach is False


def test_target_create_target_dispatches_to_the_model(rig):
    model, ext, cdp = rig
    result = asyncio.run(handle_cdp_command(model, "Target.createTarget", {"url": "https://x/"}, None))
    assert result["targetId"] == "target-100"


def test_unrecognized_command_with_no_session_forwards_as_browser_command(rig):
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://example.com/"})
    asyncio.run(model.enable_auto_attach())

    asyncio.run(handle_cdp_command(model, "Storage.clearDataForOrigin", {}, None))

    assert any(c[0] == "chrome.debugger.sendCommand" for c in ext.calls)


def test_unrecognized_command_with_a_session_forwards_as_a_tab_command(rig):
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://example.com/"})
    asyncio.run(model.enable_auto_attach())

    asyncio.run(handle_cdp_command(model, "Runtime.evaluate", {"expression": "1"}, "pw-tab-1"))

    call = [c for c in ext.calls if c[0] == "chrome.debugger.sendCommand"][-1]
    assert call[1][0] == {"tabId": 1}


def test_attach_to_target_returns_the_existing_session_for_a_known_target(rig):
    """What context.new_cdp_session(page) needs to work at all against this
    relay — it's the only way Playwright reaches a CDP method with no
    high-level wrapper (e.g. Input.setIgnoreInputEvents)."""
    model, ext, cdp = rig
    model.on_tab_created({"id": 1, "url": "https://example.com/"})
    asyncio.run(model.enable_auto_attach())
    tab_session = model._tab_sessions[1]

    result = asyncio.run(
        handle_cdp_command(
            model, "Target.attachToTarget",
            {"targetId": tab_session.target_info["targetId"]}, None,
        )
    )

    assert result == {"sessionId": tab_session.session_id}


def test_attach_to_target_raises_for_an_unknown_target_id(rig):
    model, ext, cdp = rig

    with pytest.raises(RuntimeError):
        asyncio.run(
            handle_cdp_command(model, "Target.attachToTarget", {"targetId": "no-such-target"}, None)
        )
