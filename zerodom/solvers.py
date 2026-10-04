"""Solver provider adapters — rung 3 of the clearance ladder (see clearance.py).

Uniform contract per adapter: `solve(wall_kind, url, html) -> Solution`,
implemented as createTask → poll for ready → token/cookies. Bundled day one
(T0.1): CapSolver, 2Captcha, Anti-Captcha, plus any custom endpoint — each
one a class with the same `solve()` registered in `get_solver()`, so joining
another provider changes nothing else in zerodom.

Configuration (house style: `os.environ.get("ZERODOM_*")`, same pattern as
ZERODOM_CHROME / ZERODOM_PROXY; each falls back to `~/.zerodom/config.json`
via `zerodom config set`, env wins):

    ZERODOM_SOLVER_PROVIDER   capsolver | 2captcha | anti-captcha | custom
    ZERODOM_SOLVER_KEY        the provider API key — never logged, never printed
    ZERODOM_SOLVER_URL        generic endpoint: POST {wall_kind, url}, operator-defined
    ZERODOM_NO_SOLVER=1       kill switch, beats everything

No solver fires unless the operator opts in: a provider (plus its key) and/or
a custom endpoint. `--no-solver` / ZERODOM_NO_SOLVER beat everything.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_POLL_INTERVAL_S = 1.5
# A solve that hasn't resolved by then is a lost solve; the ladder records the
# wall as blocked and moves on rather than hanging the navigation.
SOLVE_TIMEOUT_S = 60.0

_TRUE = ("1", "true", "yes", "on")


class SolverError(RuntimeError):
    """The provider refused, errored, or timed out. Never carries the API key."""


@dataclass
class Solution:
    mode: str  # "cookie" (clearance-style) | "widget" (captcha response token)
    provider: str
    token: str | None = None
    cookies: list[dict] = field(default_factory=list)
    cost: object = None  # provider-reported spend/credits, if it returned any


def _post_json(url: str, payload: dict) -> dict:
    """POST JSON, return the decoded body. Module-level so tests can monkeypatch
    the transport (CI has no external network) or point ZERODOM_SOLVER_URL at a
    local fake."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=SOLVE_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _get_json(url: str) -> dict:
    """GET JSON (2Captcha's res.php poll leg), same monkeypatch story."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=SOLVE_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# -- in-product config (zerodom config set/get) -----------------------------

CONFIG_PATH = Path.home() / ".zerodom" / "config.json"


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(cfg: dict) -> None:
    """0600 — the file holds solver keys."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass  # non-POSIX filesystem — content is still the user's own file


def _setting(env_key: str, dotted: str, cfg: dict | None = None) -> str:
    """Env beats the config file; either beats unset."""
    val = os.environ.get(env_key, "").strip()
    if val:
        return val
    if cfg is None:
        cfg = load_config()
    node: object = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return ""
        node = node[part]
    return str(node).strip()


_SITEKEY_RES = (
    re.compile(r'data-sitekey=["\']([^"\']+)["\']'),
    re.compile(r'[?&]sitekey=([0-9A-Za-z_-]{16,})'),
    re.compile(r'"sitekey"\s*:\s*"([0-9A-Za-z_-]{16,})"'),
)


def sitekey(html: str) -> str | None:
    """The challenge sitekey out of the page HTML, deterministic regex only —
    the widget markup is where every vendor parks it."""
    for pat in _SITEKEY_RES:
        if m := pat.search(html):
            return m.group(1)
    return None


class CapSolver:
    """CapSolver adapter (https://api.capsolver.com) — the day-one default."""

    provider = "capsolver"
    API = "https://api.capsolver.com"

    _TASK_TYPES = {
        "cloudflare": "AntiCloudflareTask",
        "turnstile": "AntiTurnstileTaskProxyLess",
        "recaptcha": "ReCaptchaV2TaskProxyLess",
        "hcaptcha": "AntiHCaptchaTaskProxyLess",
    }

    def __init__(self, key: str):
        self._key = key

    def solve(self, wall_kind: str, url: str, html: str = "") -> Solution:
        task: dict = {"type": self._TASK_TYPES.get(wall_kind, "AntiCloudflareTask"),
                      "websiteURL": url}
        if task["type"] != "AntiCloudflareTask":
            key = sitekey(html)
            if not key:
                raise SolverError(f"no sitekey found in the {wall_kind} page markup")
            task["websiteKey"] = key
        created = _post_json(f"{self.API}/createTask", {"clientKey": self._key, "task": task})
        if created.get("errorId"):
            raise SolverError(created.get("errorDescription") or "createTask failed")
        task_id = created["taskId"]
        deadline = time.monotonic() + SOLVE_TIMEOUT_S
        while time.monotonic() < deadline:
            res = _post_json(f"{self.API}/getTaskResult", {"clientKey": self._key, "taskId": task_id})
            if res.get("errorId"):
                raise SolverError(res.get("errorDescription") or "getTaskResult failed")
            if res.get("status") == "ready":
                return self._solution(res.get("solution") or {})
            time.sleep(_POLL_INTERVAL_S)
        raise SolverError("solver timed out waiting for a solution")

    def _solution(self, sol: dict) -> Solution:
        token = (sol.get("gRecaptchaResponse") or sol.get("token")
                 or sol.get("turnstileToken") or sol.get("resp") or sol.get("solution"))
        cookies = [dict(c) for c in (sol.get("cookies") or []) if c.get("name")]
        return Solution(
            mode="cookie" if cookies else "widget",
            provider=self.provider,
            token=token,
            cookies=cookies,
            cost=sol.get("cost", sol.get("credits")),
        )


class CustomSolver:
    """Generic operator-defined endpoint via ZERODOM_SOLVER_URL.

    Request:  POST {"wall_kind": ..., "url": ...}
    Response: {"token": str} and/or {"cookies": [{name, value, ...}]} plus
              optional {"mode": "cookie"|"widget", "cost": ...} — the rest of
              the contract is the operator's to define."""
    provider = "custom"

    def __init__(self, endpoint: str):
        self._url = endpoint

    def solve(self, wall_kind: str, url: str, html: str = "") -> Solution:
        res = _post_json(self._url, {"wall_kind": wall_kind, "url": url})
        if not isinstance(res, dict):
            raise SolverError("custom solver returned a non-object response")
        if res.get("error"):
            raise SolverError(str(res["error"]))
        cookies = [dict(c) for c in (res.get("cookies") or []) if c.get("name")]
        token = res.get("token")
        if not cookies and not token:
            raise SolverError("custom solver returned neither token nor cookies")
        mode = res.get("mode") or ("cookie" if cookies else "widget")
        return Solution(mode=mode, provider=self.provider, token=token,
                        cookies=cookies, cost=res.get("cost", res.get("credits")))


def solver_disabled() -> bool:
    """The kill switch — env only, so a host-wide `ZERODOM_NO_SOLVER=1` beats
    every per-call flag."""
    return os.environ.get("ZERODOM_NO_SOLVER", "").strip().lower() in _TRUE


class TwoCaptcha:
    """2Captcha adapter (https://2captcha.com) — in.php create / res.php poll."""

    provider = "2captcha"
    API = "https://2captcha.com"
    _METHODS = {"recaptcha": "userrecaptcha", "hcaptcha": "hcaptcha",
                "turnstile": "turnstile", "cloudflare": "cf_clearance"}

    def __init__(self, key: str):
        self._key = key

    def solve(self, wall_kind: str, url: str, html: str = "") -> Solution:
        method = self._METHODS.get(wall_kind)
        if not method:
            raise SolverError(f"2captcha: no method for a {wall_kind} wall")
        payload: dict = {"key": self._key, "method": method, "pageurl": url, "json": 1}
        if method != "cf_clearance":
            site = sitekey(html)
            if not site:
                raise SolverError(f"no sitekey found in the {wall_kind} page markup")
            payload["googlekey" if method == "userrecaptcha" else "sitekey"] = site
        created = _post_json(f"{self.API}/in.php", payload)
        if not created.get("status"):
            raise SolverError(str(created.get("request") or "in.php failed"))
        task_id = str(created["request"])
        deadline = time.monotonic() + SOLVE_TIMEOUT_S
        while time.monotonic() < deadline:
            query = urllib.parse.urlencode(
                {"key": self._key, "action": "get", "id": task_id, "json": 1})
            res = _get_json(f"{self.API}/res.php?{query}")
            request = res.get("request")
            if res.get("status") in (1, "1", True):
                token = str(request)
                if method == "cf_clearance":
                    # Their CF task returns the clearance cookie's value — hand
                    # it to rung 3 as a cookie, not as a widget response.
                    return Solution(mode="cookie", provider=self.provider, token=token,
                                    cookies=[{"name": "cf_clearance", "value": token}])
                return Solution(mode="widget", provider=self.provider, token=token)
            if request != "CAPCHA_NOT_READY":  # their spelling, not a typo on our side
                raise SolverError(str(request or "res.php failed"))
            time.sleep(_POLL_INTERVAL_S)
        raise SolverError("solver timed out waiting for a solution")


class AntiCaptcha:
    """Anti-Captcha adapter (https://api.anti-captcha.com) — createTask/getTaskResult
    shape, same as CapSolver but its own endpoint and key."""

    provider = "anti-captcha"
    API = "https://api.anti-captcha.com"
    _TASK_TYPES = {"recaptcha": "NoCaptchaTaskProxyLess",
                   "hcaptcha": "HCaptchaTaskProxyLess",
                   "turnstile": "AntiTurnstileTaskProxyLess"}

    def __init__(self, key: str):
        self._key = key

    def solve(self, wall_kind: str, url: str, html: str = "") -> Solution:
        task_type = self._TASK_TYPES.get(wall_kind)
        if not task_type:
            # No proxyless task for a full CF interstitial — say so instead of
            # inventing one; the rung records the miss with this as its note.
            raise SolverError(
                f"anti-captcha has no proxyless task for a {wall_kind} wall "
                "— use capsolver or ZERODOM_SOLVER_URL")
        site = sitekey(html)
        if not site:
            raise SolverError(f"no sitekey found in the {wall_kind} page markup")
        created = _post_json(f"{self.API}/createTask", {
            "clientKey": self._key,
            "task": {"type": task_type, "websiteURL": url, "websiteKey": site},
        })
        if created.get("errorId"):
            raise SolverError(created.get("errorDescription") or "createTask failed")
        task_id = created["taskId"]
        deadline = time.monotonic() + SOLVE_TIMEOUT_S
        while time.monotonic() < deadline:
            res = _post_json(f"{self.API}/getTaskResult",
                             {"clientKey": self._key, "taskId": task_id})
            if res.get("errorId"):
                raise SolverError(res.get("errorDescription") or "getTaskResult failed")
            if res.get("status") == "ready":
                sol = res.get("solution") or {}
                token = sol.get("gRecaptchaResponse") or sol.get("token")
                return Solution(mode="widget", provider=self.provider, token=token,
                                cost=sol.get("cost"))
            if res.get("status") != "processing":
                raise SolverError(str(res.get("status") or "getTaskResult failed"))
            time.sleep(_POLL_INTERVAL_S)
        raise SolverError("solver timed out waiting for a solution")


def get_solver():
    """The configured adapter (CapSolver/2Captcha/AntiCaptcha/CustomSolver), or
    None when the operator hasn't opted in. Env beats ~/.zerodom/config.json."""
    if solver_disabled():
        return None
    cfg = load_config()
    provider = _setting("ZERODOM_SOLVER_PROVIDER", "solver.provider", cfg).lower()
    endpoint = _setting("ZERODOM_SOLVER_URL", "solver.url", cfg)
    key = _setting("ZERODOM_SOLVER_KEY", "solver.key", cfg)
    if provider in ("", "none", "off", "disabled", "0"):
        # Opting in with a URL alone is explicit enough to count; a stray key
        # with no provider does not (and an unset provider must stay off).
        return CustomSolver(endpoint) if endpoint else None
    if provider in ("custom", "endpoint"):
        return CustomSolver(endpoint) if endpoint else None
    if provider == "capsolver":
        return CapSolver(key) if key else None
    if provider in ("2captcha", "2-captcha"):
        return TwoCaptcha(key) if key else None
    if provider in ("anti-captcha", "anticaptcha", "anti_captcha"):
        return AntiCaptcha(key) if key else None
    return None  # unknown provider: off, never a crash mid-navigation
