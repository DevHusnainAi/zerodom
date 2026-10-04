"""Challenge clearance ladder — rungs tried in order until a bot wall drops.

    attempt_clearance(page, wall_kind, policy) -> dict | None   (awaitable on async pages)

    rung 1 "reused"   carried clearance (cf_clearance cookie / storage-state /
                      relay session) or the wall resolving on its own — a
                      bounded reload/poll, no new state.
    rung 2 "stealth"  cdp_pipe transport only: the hygiene (DOM-domain reads,
                      scrubbed launch flags — see cdp_pipe.spawn/outer_html) IS
                      the fix, so this rung re-loads there and reports clean.
                      On the Playwright paths it is a documented ceiling.
    rung 3 "solver"   provider adapter (solvers.py): createTask → poll →
                      inject cookie/token → re-nav → re-check. Opt-in keys,
                      scope-gated like every other action, spend logged in the
                      returned metadata.
    rung 4 "human"    relay transport: surface the widget selector so the
                      operator clears it in their own browser, then re-check
                      within a short bounded window.

Success returns {"rung", "wall_kind", "attempts", "seconds"} (plus "solver"/
"note" when the rung produced them) — the caller attaches it as
`metadata["cleared"]`. All rungs failing returns None and the caller keeps
`metadata["blocked"]` (honesty: a wall we can't clear stays recorded as a miss).

The choke points are exactly three, each calling this post-goto, pre-parse:
`cli._goto`, `mcp_server._goto`, `cdp_pipe.CDPPipe.outer_html`. parser.py stays
browser/network-free: this module calls *into* parser (`_detect_challenge`),
never the reverse. Results travel to the parse site through the module slot
(`take_cleared`/`take_notes`) because the transports' return contracts —
`fetch() -> (html, final_url)`, `outer_html() -> (html, final_url)` — are fixed.
Slot assumes one navigation at a time, which is what the single global MCP
session and the one-command-at-a-time CLI already guarantee.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import time
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Any

from .parser import _detect_challenge
from .scope import check_scope, load_scope
from . import solvers

# Bounded waits. A ladder that hangs is worse than a recorded miss: CF
# interstitials resolve in seconds if they resolve at all, and rung 4's window
# exists to catch a human who is already mid-click, not to wait one out.
REUSE_WAIT_S = 3.0
HUMAN_WAIT_S = 5.0
_POLL_S = 0.5
_RENAV_TIMEOUT_MS = 30000

# Cookie names that mean "this session already passed a challenge once".
_CLEARANCE_COOKIES = ("cf_clearance",)

_RESPONSE_FIELDS = {
    "turnstile": "cf-turnstile-response",
    "recaptcha": "g-recaptcha-response",
    "hcaptcha": "h-captcha-response",
}


@dataclass
class Policy:
    """What the ladder is allowed to do on this navigation.

    `transport` decides rung 2/4 availability ("playwright" | "cdp_pipe" |
    "relay"). `scope` is the MCP-style {allow, deny} dict; None = unrestricted
    (same semantics as MCP with no scope). `rungs` restricts the ladder to a
    subset (benchmark per-rung cells, tests). `notes` collects human-facing
    messages — the caller owns the policy, so it owns the output channel.
    """
    transport: str = "playwright"
    no_solver: bool = False
    url: str | None = None
    scope: dict | None = None
    rungs: tuple[str, ...] | None = None
    notes: list[str] = field(default_factory=list)


_DEFAULTS: dict[str, Any] = {"no_solver": False}


def set_defaults(**kw: Any) -> None:
    """Process-wide policy defaults — the CLI sets `no_solver` from --no-solver
    in main(), before any navigation (crawl_site builds its own call path)."""
    _DEFAULTS.update(kw)


def make_policy(**kw: Any) -> Policy:
    if "scope" not in kw and os.environ.get("ZERODOM_SCOPE"):
        kw["scope"] = load_scope(os.environ["ZERODOM_SCOPE"])
    kw.setdefault("no_solver", bool(_DEFAULTS.get("no_solver")))
    return Policy(**kw)


# -- result slot -----------------------------------------------------------

_LAST: dict | None = None
_NOTES: list[str] = []


def _record(result: dict | None) -> None:
    global _LAST
    _LAST = result


def _record_notes(notes: list[str]) -> None:
    _NOTES[:] = list(notes)


def take_cleared() -> dict | None:
    """The last ladder result (or None), consumed — one parse takes it once."""
    global _LAST
    out, _LAST = _LAST, None
    return out


def take_notes() -> list[str]:
    """Human-facing ladder messages for the last navigation, consumed."""
    out = list(_NOTES)
    _NOTES.clear()
    return out


# -- transport plumbing -----------------------------------------------------

def _camel(k: str) -> str:
    head, *rest = k.split("_")
    return head + "".join(s[:1].upper() + s[1:] for s in rest)


async def _aw(obj: Any, name: str, *args: Any, **kw: Any) -> Any:
    """Call a page/context method and await it only if it is awaitable.

    On the sync-Playwright path the ladder coroutine rides Playwright's own
    loop (see `_run_sync`), so a sync wrapper's method — which drives the
    greenlet ping-pong and cannot run from inside that loop — is bypassed for
    the underlying impl (`_impl_obj`), exactly what the generated async API
    wraps. The impl speaks the protocol's camelCase kwargs, so keyword names
    are translated on that branch; positional args and results pass through
    (every value this ladder uses — str/list/None/Any — needs no mapping).
    Everywhere else the wrapper method is called directly and duck-awaited, so
    one ladder body still drives sync Playwright, async Playwright and the raw
    CDP page view."""
    impl = getattr(obj, "_impl_obj", None)
    loop = getattr(obj, "_loop", None)
    if impl is not None and loop is not None and asyncio.get_running_loop() is loop:
        out = getattr(impl, name)(*args, **{_camel(k): v for k, v in kw.items()})
    else:
        out = getattr(obj, name)(*args, **kw)
    if inspect.isawaitable(out):
        out = await out
    return out


def _is_async_page(page: Any) -> bool:
    for name in ("wait_for_timeout", "content", "goto"):
        meth = getattr(page, name, None)
        if meth is not None:
            return inspect.iscoroutinefunction(meth)
    return False


def _run_sync(page: Any, coro: Any) -> Any:
    """Drive the ladder coroutine for a *sync* page to completion.

    Playwright's sync API owns one loop and one dispatcher greenlet per
    thread, and leaks its loop as the thread's "running loop" after every
    call — a foreign `asyncio.run` on this thread fails twice over (refused
    at entry; greenlet cross-thread switch if moved to a worker). So the
    coroutine rides Playwright's own loop instead, driven exactly the way
    `SyncBase._sync` drives its own tasks: schedule, pump the dispatcher
    until done, hop back through a done-callback. The rungs then talk to the
    browser via `_aw`'s impl branch — no nested greenlet switch anywhere.

    Non-Playwright sync pages (test fakes, the CDP pipe view) have no loop to
    borrow and get a plain asyncio.run, with any leaked running-loop masked
    for its duration and restored after."""
    loop = getattr(page, "_loop", None)
    dispatcher = getattr(page, "_dispatcher_fiber", None)
    if loop is None or dispatcher is None:
        try:
            prev = asyncio.get_running_loop()
        except RuntimeError:
            prev = None
        asyncio._set_running_loop(None)
        try:
            return asyncio.run(coro)
        finally:
            asyncio._set_running_loop(prev)
    import greenlet  # present whenever Playwright's sync API is

    g_self = greenlet.getcurrent()
    try:
        task = loop.create_task(coro)
    except Exception:
        coro.close()
        raise
    task.add_done_callback(lambda _: g_self.switch())
    while not task.done():
        if dispatcher.dead:
            raise RuntimeError(
                "playwright dispatcher stopped before the clearance ladder finished")
        dispatcher.switch()
    return task.result()


# -- shared checks ----------------------------------------------------------

def _cleared_html(html: str, kind: str) -> bool:
    """Is the page past the challenge? Marker-gone for a full-page wall; for a
    widget wall the markers legitimately stay in the markup forever, so the
    signal is a filled response field (which is why injection sets textContent
    — page.content() serializes attributes/children, not JS properties)."""
    if _detect_challenge(html) is None:
        return True
    field = _RESPONSE_FIELDS.get(kind)
    if not field:
        return False
    esc = field.replace("-", r"\-")
    for pat in (rf'name="{esc}"[^>]*>([^<]+)',
                rf'name="{esc}"[^>]*value="([^"]+)"'):
        if m := re.search(pat, html):
            if m.group(1).strip():
                return True
    return False


async def _fresh_cleared(ctx: _Ctx) -> bool:
    ctx.html = await _aw(ctx.page, "content")
    return _cleared_html(ctx.html, ctx.kind)


async def _poll_cleared(ctx: _Ctx, seconds: float) -> bool:
    """Bounded re-check: at least once, then every _POLL_S until the deadline."""
    deadline = time.monotonic() + seconds
    while True:
        if await _fresh_cleared(ctx):
            return True
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        await asyncio.sleep(min(_POLL_S, left))


async def _carries_clearance(page: Any) -> bool:
    browser_ctx = getattr(page, "context", None)
    if browser_ctx is None:
        return False
    try:
        cookies = await _aw(browser_ctx, "cookies")
    except Exception:
        return False
    return any(c.get("name") in _CLEARANCE_COOKIES for c in cookies or [])


async def _renav(ctx: _Ctx) -> None:
    if not ctx.url:
        return
    await _aw(ctx.page, "goto", ctx.url, wait_until="domcontentloaded",
              timeout=_RENAV_TIMEOUT_MS)
    ctx.html = await _aw(ctx.page, "content")


def _widget_hint(html: str, kind: str) -> str | None:
    """Selector for the widget the operator has to click, from markup only —
    cheaper than a frame walk and it works before the graph exists."""
    probes = {
        "turnstile": ("challenges.cloudflare.com/turnstile", 'iframe[src*="challenges.cloudflare.com"]'),
        "recaptcha": ("recaptcha", 'iframe[src*="google.com/recaptcha"]'),
        "hcaptcha": ("hcaptcha.com", 'iframe[src*="hcaptcha.com"]'),
    }
    probe, sel = probes.get(kind, ("", ""))
    return sel if probe and probe in html.lower() else None


# -- rungs ------------------------------------------------------------------

async def _rung_reused(ctx: _Ctx) -> dict | None:
    # The wall may already be gone (caller saw it one read ago and the carried
    # session cleared it during this nav) — confirm before doing anything.
    if _cleared_html(ctx.html, ctx.kind):
        return {}
    if await _carries_clearance(ctx.page):
        await _renav(ctx)
        if await _fresh_cleared(ctx):
            return {}
    # Cloudflare interstitials self-resolve in place when the browser runs the
    # challenge JS with a carried cookie — give it a bounded window. Widget
    # walls never self-flip, so polling them would just burn the wait.
    if ctx.kind == "cloudflare" and await _poll_cleared(ctx, REUSE_WAIT_S):
        return {}
    return None


async def _rung_stealth(ctx: _Ctx) -> dict | None:
    # ponytail: on the Playwright paths this rung cannot exist — Playwright's
    # own driver sends Runtime.enable/evaluate while the page loads and
    # vendors fingerprint it (microsoft/playwright#34025), outside our control.
    # The hygiene lives entirely on the cdp_pipe transport (DOM-domain reads,
    # scrubbed launch flags), so the rung means: reload under that transport
    # and report whether the wall came back clean.
    if ctx.policy.transport != "cdp_pipe":
        return None
    await _renav(ctx)
    if await _fresh_cleared(ctx):
        return {}
    return None


_INJECT_JS = """([field, token]) => {
  const els = [...document.querySelectorAll(`textarea[name="${field}"], input[name="${field}"]`)];
  let done = false;
  for (const el of els) {
    el.value = token;
    if (el.tagName === "TEXTAREA") el.textContent = token;
    else el.setAttribute("value", token);
    for (const type of ["input", "change"]) el.dispatchEvent(new Event(type, {bubbles: true}));
    done = true;
  }
  const widget = document.querySelector(".g-recaptcha, .cf-turnstile, .h-captcha");
  const cb = widget && widget.getAttribute("data-callback");
  if (cb && typeof window[cb] === "function") { window[cb](token); done = true; }
  return done;
}"""


async def _add_cookies(ctx: _Ctx, cookies: list[dict]) -> None:
    browser_ctx = getattr(ctx.page, "context", None)
    if browser_ctx is None:
        raise solvers.SolverError("page exposes no cookie context for clearance cookies")
    jar = []
    for c in cookies:
        c = dict(c)
        if "url" not in c and "domain" not in c:
            # Playwright wants url *or* domain(+path) — never both (it rejects
            # url+path outright). A bare path rides with a derived domain;
            # otherwise url-mode, which derives the path itself.
            if "path" in c:
                c["domain"] = urlsplit(ctx.url).hostname or "localhost"
            else:
                c["url"] = ctx.url
        if "domain" in c:
            c.setdefault("path", "/")
        jar.append(c)
    await _aw(browser_ctx, "add_cookies", jar)


async def _rung_solver(ctx: _Ctx) -> dict | None:
    p = ctx.policy
    if p.no_solver or solvers.solver_disabled():
        return None
    try:
        check_scope(ctx.url, p.scope)
    except PermissionError as exc:
        p.notes.append(str(exc))
        return None
    solver = solvers.get_solver()
    if solver is None:
        p.notes.append(
            "no solver configured — set ZERODOM_SOLVER_PROVIDER + ZERODOM_SOLVER_KEY "
            "(or ZERODOM_SOLVER_URL) to enable rung 3, or clear the challenge by hand "
            "(relay mode / --storage-state)."
        )
        return None
    try:
        # asyncio.to_thread: on the MCP path a synchronous provider poll must
        # not freeze the event loop for the whole solve.
        sol = await asyncio.to_thread(solver.solve, ctx.kind, ctx.url, ctx.html)
        if sol.cookies:
            await _add_cookies(ctx, sol.cookies)
        # Always re-nav: a reload is what makes the browser *send* clearance
        # cookies, and it gets a fresh document for the token path (a nav after
        # injecting a widget token would wipe the response field — which is why
        # widget injection runs below, post-nav; reCAPTCHA tokens die in ~2min
        # either way, so injection stays the last step before the check).
        await _renav(ctx)
        if sol.token:
            field = _RESPONSE_FIELDS.get(ctx.kind, "g-recaptcha-response")
            await _aw(ctx.page, "evaluate", _INJECT_JS, [field, sol.token])
        if await _fresh_cleared(ctx):
            info: dict[str, Any] = {"provider": sol.provider}
            if sol.cost is not None:
                info["cost"] = sol.cost
            return {"solver": info}
    except Exception as exc:  # SolverError/transport — the wall stays `blocked`
        p.notes.append(f"solver rung failed: {exc}")
    return None


async def _rung_human(ctx: _Ctx) -> dict | None:
    if ctx.policy.transport != "relay":
        return None
    hint = _widget_hint(ctx.html, ctx.kind)
    msg = (f"{ctx.kind} challenge at {ctx.url}"
           + (f" — clear it in your browser tab (widget: {hint})" if hint
              else " — clear it in your browser tab")
           + "; the ladder re-checks for a few seconds and resumes once it's gone.")
    ctx.policy.notes.append(msg)
    if await _poll_cleared(ctx, HUMAN_WAIT_S):
        return {"note": msg}
    return None


# Ordered: this dict IS the ladder.
_RUNGS: dict = {
    "reused": _rung_reused,
    "stealth": _rung_stealth,
    "solver": _rung_solver,
    "human": _rung_human,
}


@dataclass
class _Ctx:
    page: Any
    kind: str
    policy: Policy
    html: str
    url: str


async def _attempt(page: Any, wall_kind: str | None, policy: Policy) -> dict | None:
    t0 = time.monotonic()
    try:
        html = await _aw(page, "content")
        detected = _detect_challenge(html)
        kind = (detected or {}).get("kind") or wall_kind
        if kind is None:
            return None  # no wall on this nav — nothing to clear
        ctx = _Ctx(page=page, kind=kind, policy=policy, html=html,
                   url=policy.url or getattr(page, "url", None) or "")
        attempts = 0
        for name, rung in _RUNGS.items():
            if policy.rungs is not None and name not in policy.rungs:
                continue
            attempts += 1
            extra = await rung(ctx)
            if extra is not None:
                result = {"rung": name, "wall_kind": kind, "attempts": attempts,
                          "seconds": round(time.monotonic() - t0, 2), **extra}
                _record(result)
                return result
        return None
    except Exception as exc:
        # A best-effort ladder must never fail the navigation it rode in on:
        # record why and hand the wall back as a plain `blocked` miss.
        policy.notes.append(f"clearance ladder error: {type(exc).__name__}: {exc}")
        return None
    finally:
        _record_notes(policy.notes)


def attempt_clearance(page: Any, wall_kind: str | None, policy: Policy | None = None):
    """Run the rung ladder against `page`. Returns the `metadata["cleared"]`
    dict, or None when every rung fails (the caller keeps `blocked`).

    Returns an awaitable when `page` is an async Playwright page (the MCP
    path awaits it); a plain dict|None for sync pages — same split
    `playwright_wrapper.ZeroDOM.from_page` uses.
    """
    _record(None)
    _record_notes([])
    if not callable(getattr(page, "content", None)):
        return None  # a fake/minimal page without DOM access — nothing to check
    policy = policy or make_policy()
    coro = _attempt(page, wall_kind, policy)
    if _is_async_page(page):
        return coro
    return _run_sync(page, coro)
