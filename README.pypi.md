# ZeroDOM

<!-- mcp-name: io.github.DevHusnainAi/zerodom -->

[![PyPI](https://img.shields.io/pypi/v/zerodom?label=pypi&color=3775A9)](https://pypi.org/project/zerodom/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/DevHusnainAi/zerodom/blob/main/LICENSE)
[![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/tVRznUb5Ez)
[![Glama](https://glama.ai/mcp/servers/DevHusnainAi/zerodom/badges/score.svg)](https://glama.ai/mcp/servers/DevHusnainAi/zerodom)

### Point it at an app as two users. It finds the broken access control.

ZeroDOM drives your **real, logged-in browser** as two identities, replays the same requests
as each, and flags where one user reaches what another can't — **IDOR / broken access control**,
caught deterministically. Open-core, runs local, no account, no API key, nothing leaves your machine.

> Under the hunt is a deterministic, token-lean perception engine for AI agents — the same core
> powers `scan`, `crawl` and any browser-driving task. IDOR is just where it bites hardest.

![A normal user is denied the admin console (401), then reaches it by flipping one client-controlled cookie. ZeroDOM replays the same request as both identities and reports the 401-vs-200 split.](https://raw.githubusercontent.com/DevHusnainAi/zerodom/v0.1.1/assets/idor-demo.gif)

## Quickstart

**See it run in 10 seconds** — no account, no browser download (bare `zerodom <url>` is a static fetch):

```bash
pip install zerodom
zerodom https://news.ycombinator.com     # the interaction graph + a token report
```

**Find an IDOR.** Save two logged-in sessions as Playwright `storage_state` files (one per
account), then diff a protected endpoint across both:

```bash
zerodom compare https://app.example.com/api/admin/dashboard \
  --as user=user.json --as admin=admin.json
```

```jsonc
{
  "identities": { "user": { "status": 401 }, "admin": { "status": 200 } },
  "only_admin": ["Manage users", "Billing & plans", "Overview"],
  "note": "admin authorized (200) where user denied (401) — broken access control if the only difference is client-controllable"
}
```

Same request, two identities, a 401-vs-200 split → a lead to confirm. No LLM guessing: ZeroDOM
surfaces the tell with the exact replay that proves it. **You confirm the bug.**

## How it hunts

- **`zerodom compare`** — one endpoint across identities: a 401-vs-200 split, or a byte-identical
  cross-tenant response, is the IDOR tell.
- **`zerodom sweep URL --as user=… --as admin=…`** — the whole app in one command: crawls as one
  identity to map the GET surface, replays every in-scope URL across all identities, reports only
  the deterministic tells (`--format md` for a report table).
- **`zerodom crawl` / `scan` / `audit`** — recon: routes/forms/API plus the callable API surface
  (method + body) and DOM-XSS sinks read from the page's JS, response-header posture (CSP/CORS/cookie)
  and a `csrf-posture` correlation, and resolving the selectors in an existing test suite.
- **`zerodom repeat` / `probe` / `race`** — act & verify: a Repeater (`--save` HAR/.http evidence),
  a tampered-body diff for mass assignment (`probe --tamper`), and a concurrent burst to flag a
  check-then-act race (`race`; HTTP/2 multiplexing via the `zerodom[race]` extra).

It drives your **real Chrome** (relay mode) over `chrome.debugger` — cookies, MFA, SSO already
done — or a throwaway browser (`--stealth`). Read-only by default; scope is enforceable and
lockable. Route through Burp/Caido with `--proxy`. *Only test targets you're authorized to.*

## From an AI agent (MCP)

ZeroDOM is a native **MCP server**. Claude, Cursor, LangGraph, CrewAI, the OpenAI Agents SDK,
Pydantic AI and more load the full 39-tool set from one config:

```jsonc
{ "mcpServers": { "zerodom": { "command": "uvx", "args": ["--from", "zerodom", "zerodom-mcp"] } } }
```

Run `zerodom setup` for a guided first-run. Runnable per-framework examples:
[examples/integrations/](https://github.com/DevHusnainAi/zerodom/tree/main/examples/integrations).

## Why the agent sees so few tokens

A live page becomes a flat list where every entry has an id that resolves to **exactly one
element**, and the CSS selector never enters the model's context:

```text
[01] a 'Show HN: ZeroDOM — agents only need to know what they can click'
[02] a 'dev'
[03] a '214 comments'
```

Deterministic (lxml in, graph out, no LLM). Measured and re-runnable: **~71% fewer tokens** than
Playwright MCP's `aria_snapshot(mode="ai")` (mean; ~53–96% per site), **99.00%** of 10,756 audited
selectors across 111 live sites resolve to exactly one element, parse median **55ms**.

## Open core

Everything in the package — parser, CLI, relay, stealth, scan/crawl/compare/audit, the clearance
ladder, the MCP server — is the free, Apache-2.0 **Core**, complete on its own: one operator,
local, no account, no key. **Enterprise** is an additive team layer (fleet dashboard, compliance
export packs, SSO/RBAC, SLA) that never gates, phones home from, or adds telemetry to the Core.
Details: **contact@vexralabs.com**.

---

**[GitHub](https://github.com/DevHusnainAi/zerodom)** · **[Docs](https://zerodom.vexralabs.com/docs)** · **[Site](https://zerodom.vexralabs.com)** · **[Discord](https://discord.gg/tVRznUb5Ez)** — Apache-2.0, a Vexra Labs project.
