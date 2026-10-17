# ZeroDOM × agent frameworks

**You don't write a ZeroDOM adapter per framework.** ZeroDOM is already an MCP server
(`zerodom-mcp`), and every modern agent framework loads MCP servers natively — so integrating
is the *same config* everywhere, plus a few framework-specific lines to build the agent.

Every example here loads the **full 35-tool set** — not just perception (`parse_url`, `find`,
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

Install per file is in that file's docstring; each is a standalone, copy-paste script.

## Other frameworks

The same stdio config works anywhere with MCP support — **LlamaIndex** (`McpToolSpec`),
**Google ADK** (`MCPToolset`), and the IDE agents (**Cursor**, **Cline**, **Continue**,
**Windsurf**, **VS Code / Copilot**, **Zed**) that take the raw config in their MCP settings.
For those IDEs, drop the config under `mcpServers` (the exact JSON is in the
[main README](../../README.md#use-it-from-an-ai-agent-mcp)).

---

*All of these drive a browser and replay authenticated requests. Point them only at targets you
are authorized to assess.* The example task uses `./user.json` and `./admin.json` — Playwright
`storage_state` files, one per logged-in account.
