# Session reuse: headless vs stealth vs relay

`uv run benchmarks/benchmark_session.py` writes this table;
`ZERODOM_E2E=1` adds the live target. One row per (target, transport).

| target | transport | nodes | wall | landed | seconds | note |
|---|---|---|---|---|---:|---|
| fixture/public | headless | 6 | — | /public | 1.8 |  |
| fixture/public | stealth | 6 | — | /public | 2.8 |  |
| fixture/public | relay | — | — | — | — | --relay needs a relay endpoint — run `zerodom relay` with the ZeroDOM extension connected (or set ZERODOM_CDP_ENDPOINT) and retry |
| fixture/authed | headless | 4 | — | /login | 2.28 |  |
| fixture/authed | stealth | — | — | — | — | error: timed out waiting for a CDP frame from chrome |
| fixture/authed | relay | — | — | — | — | --relay needs a relay endpoint — run `zerodom relay` with the ZeroDOM extension connected (or set ZERODOM_CDP_ENDPOINT) and retry |
| fixture/wall | headless | 2 | turnstile | /wall | 1.87 |  |
| fixture/wall | stealth | 2 | turnstile | /wall | 1.32 |  |
| fixture/wall | relay | — | — | — | — | --relay needs a relay endpoint — run `zerodom relay` with the ZeroDOM extension connected (or set ZERODOM_CDP_ENDPOINT) and retry |

## Reading this

- **fixture/authed** is the authenticated-route ground truth: without the
  session cookie the fresh-profile legs follow the 302 and land on
  `/login`; the relay leg lands on `/authed` because the cookie rides
  with the browser (seeded by this script for the fixture, removed
  afterwards; live rows use the operator's real session as-is).
- **fixture/wall** carries inline Turnstile markers: a carried session
  does not clear a wall -- every leg keeps `metadata["blocked"]`, and
  clearing is the clearance ladder's job (benchmark_clearance.py / §3.6).
- **skipped**: no relay listening or no extension attached (this script
  never spawns one, never closes the operator's browser), or no Chrome
  binary for the stealth leg (`ZERODOM_CHROME=<path>` picks one).
- seconds are wall-clock per fetch, including browser launch on the
  fresh-profile legs -- that cost is what relay reuse skips.

## Run

- date: 2026-10-04 23:31:04 +0500
- e2e: 0 (fixture only)
- chrome: /home/husnain/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome
- no_solver: 1

