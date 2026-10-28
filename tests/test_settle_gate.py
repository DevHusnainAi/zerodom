"""Unit checks for the network-idle gate + navigation retry in from_page.

Pure stubs, no browser — asserts the control flow the 100-site study exposed:
a late data fetch needs a bounded networkidle wait before serialize, and a
navigation mid-settle must retry once (not silently return an empty read).
"""

from zerodom import ZeroDOM

_HTML = "<html><body><a href='/a'>A</a></body></html>"


def _tag(fn: str) -> str:
    if "new Promise" in fn:
        return "settle"
    if fn.strip().startswith("() =>"):
        return "hydration"
    return "serialize"


class _Stub:
    """Sync Playwright-ish page recording call order."""

    def __init__(self, settle_raises: int = 0):
        self.url = "http://x"
        self.calls: list[str] = []
        self._settle_raises = settle_raises
        self.settle_attempts = 0

    def wait_for_load_state(self, state, timeout=None):
        self.calls.append(f"wait:{state}:{timeout}")

    def evaluate(self, fn, arg=None):
        tag = _tag(fn)
        if tag == "settle":
            self.settle_attempts += 1
            if self.settle_attempts <= self._settle_raises:
                raise RuntimeError("Execution context was destroyed")
            return None
        self.calls.append(f"eval:{tag}")
        if tag == "hydration":
            return {"hydrationPending": False}
        return _HTML

    def content(self):
        return _HTML


def test_networkidle_gate_runs_before_serialize():
    p = _Stub()
    ZeroDOM.from_page(p)
    assert p.calls[0] == "wait:networkidle:3000"       # gate first
    assert "eval:serialize" in p.calls
    assert p.calls.index("wait:networkidle:3000") < p.calls.index("eval:serialize")


def test_zero_cap_disables_the_gate():
    p = _Stub()
    ZeroDOM.from_page(p, settle_network_ms=0)
    assert not any(c.startswith("wait:") for c in p.calls)


def test_settle_false_skips_both_gate_and_settle():
    p = _Stub()
    ZeroDOM.from_page(p, settle=False)
    assert not any(c.startswith("wait:") for c in p.calls)
    assert p.settle_attempts == 0


def test_navigation_mid_settle_retries_once_then_succeeds():
    p = _Stub(settle_raises=1)  # first SETTLE throws (navigation), retry works
    graph = ZeroDOM.from_page(p)
    assert p.settle_attempts == 2
    assert "settle_error" not in graph["metadata"]


def test_persistent_settle_failure_is_flagged_not_silent():
    p = _Stub(settle_raises=99)  # both attempts throw
    graph = ZeroDOM.from_page(p)
    assert p.settle_attempts == 2                       # retried exactly once
    assert graph["metadata"]["settle_error"] is True    # honest flag, not empty success


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print("ok")
