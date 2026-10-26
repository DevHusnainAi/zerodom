# ZeroDOM × agent frameworks

**You don't write a ZeroDOM adapter per framework.** ZeroDOM is already an MCP server
(`zerodom-mcp`), and every modern agent framework loads MCP servers natively — so integrating
is the *same config* everywhere, plus a few framework-specific lines to build the agent.

Every example here loads the **full 39-tool set** — not just perception (`parse_url`, `find`,
`click`), but the authz weapons too: `add_identity`, `compare_identities`, `replay`, `crawl`,
`sweep`. (The [`../langchain_agent.py`](../langchain_agent.py) example instead wraps only
open/click/fill from the ZeroDOM *library* — use that when you already own the browser loop and
just want the token-lean graph.)

## The one config everyone reuses

```python
{"command": "uvx", "args": ["--from", "zerodom", "zerodom-mcp"], "transport": "stdio"}
```

`uvx` runs the server straight off PyPI. Swap for `{"command": "zerodom-mcp"}` if it's installed,
or run `zerodom relay` + the Chrome extension first so it drives your **real, logged-in** browser
(see [MCP setup](../../README.md#use-it-from-an-ai-agent-mcp)).

## ⚠️ The one gotcha: keep a single session

`zerodom-mcp` holds **one browser session + your registered identities in-process**. Most MCP
clients default to *stateless* (a fresh server session per tool call), which would drop the page,
the identities and the tab state **between steps** and break a multi-step hunt. Every example
below keeps **one `zerodom-mcp` process alive for the whole agent run** — it's the detail generic
MCP tutorials miss, and it matters here.

## The examples

| framework | file | tool loading | how the session is kept alive |
|---|---|---|---|
| **LangGraph / LangChain** | [`langgraph.py`](langgraph.py) | `MultiServerMCPClient` → `client.session()` → `load_mcp_tools` | explicit `async with client.session(...)` (the default is stateless — **don't** use `get_tools()` alone) |
| **OpenAI Agents SDK** | [`openai_agents.py`](openai_agents.py) | `MCPServerStdio`, `Agent(mcp_servers=[…])` | run inside `async with MCPServerStdio(...)` |
| **CrewAI** | [`crewai.py`](crewai.py) | `MCPServerAdapter` + `StdioServerParameters`, `Agent(tools=…)` | build + `kickoff()` inside `with MCPServerAdapter(...)` |
| **Pydantic AI** | [`pydantic_ai.py`](pydantic_ai.py) | `MCPToolset(StdioTransport(...))`, `Agent(toolsets=[…])` | run inside `async with agent` |
| **LlamaIndex** | [`llamaindex.py`](llamaindex.py) | `BasicMCPClient` + `McpToolSpec.to_tool_list_async()`, `FunctionAgent` | keep one `BasicMCPClient` for the run |
| **Google ADK** | [`google_adk.py`](google_adk.py) | `MCPToolset(StdioConnectionParams(...))`, `LlmAgent(tools=[…])` | one `MCPToolset` instance (ADK manages + closes it) |
| **AutoGen** | [`autogen.py`](autogen.py) | `mcp_server_tools(StdioServerParams(...))`, `AssistantAgent(tools=…)` | pin **`mcp<2`**; prefer `McpWorkbench` for a long hunt |

Install per file is in that file's docstring; each is a standalone, copy-paste script. Every
one is runtime-verified to connect to `zerodom-mcp` and load all 39 tools.

> **AutoGen note:** `autogen-ext` (≤0.7.5) targets the mcp **1.x** SDK — the 2.x SDK removed a
> symbol it imports (`ImportError: RequestContext`). Install `"mcp<2"` until it updates.

## IDE / editor agents (config, no code)

IDE agents (**Cursor, Cline, Continue, Windsurf, VS Code / Copilot, Zed**, plus MCP clients like
Claude Desktop / Claude Code) aren't libraries — there's nothing to write. You add `zerodom-mcp`
to the editor's MCP settings and its built-in agent gains all 39 tools. **Most** use the standard
`mcpServers` JSON:

```json
{ "mcpServers": { "zerodom": { "command": "uvx", "args": ["--from", "zerodom", "zerodom-mcp"] } } }
```

| Editor | Where it goes | Key |
|---|---|---|
| **Cursor** | `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` | `mcpServers` |
| **Cline** | "MCP Servers" panel → `cline_mcp_settings.json` | `mcpServers` |
| **Windsurf** | `~/.codeium/windsurf/mcp_config.json` (Cascade → MCP) | `mcpServers` |
| **Claude Desktop** | `claude_desktop_config.json` | `mcpServers` |
| **Claude Code** | `.mcp.json` (project), or `claude mcp add` | `mcpServers` |

**Three use a different shape** — copy these verbatim instead:

**VS Code / Copilot** — `.vscode/mcp.json` (key is `servers`, each needs `type`):
```json
{ "servers": { "zerodom": { "type": "stdio", "command": "uvx", "args": ["--from", "zerodom", "zerodom-mcp"] } } }
```

**Zed** — `settings.json` (key is `context_servers`):
```json
{ "context_servers": { "zerodom": { "command": "uvx", "args": ["--from", "zerodom", "zerodom-mcp"], "env": {} } } }
```

**Continue** — `~/.continue/config.yaml` (a YAML list under `mcpServers`):
```yaml
mcpServers:
  - name: zerodom
    command: uvx
    args: ["--from", "zerodom", "zerodom-mcp"]
```

> MCP config schemas shift between editor versions — if the tools don't show up, check your
> editor's current MCP docs. Each gives the agent the same 39 tools the framework examples load.
> To drive your *real, logged-in* Chrome, run `zerodom relay` + the extension first (see the
> [main README](../../README.md#use-it-from-an-ai-agent-mcp)).

---

*All of these drive a browser and replay authenticated requests. Point them only at targets you
are authorized to assess.* The example task uses `./user.json` and `./admin.json` — Playwright
`storage_state` files, one per logged-in account.
