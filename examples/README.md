# Examples

## Framework integrations (the full toolset, via MCP) → [`integrations/`](integrations/)

The best way to use ZeroDOM from an agent framework is **MCP** — it's already an MCP server, so
every framework loads the *whole* 35-tool set (perception **+** the `compare`/`sweep`/`replay`
IDOR tools) from one shared config, with near-zero glue. Ready-to-run examples for **LangGraph /
LangChain, OpenAI Agents SDK, CrewAI, and Pydantic AI** (plus the one gotcha that matters for
ZeroDOM — keeping a single session) live in [`integrations/`](integrations/).

## Library examples (perception only, no MCP)

The two scripts below take the other route: import the ZeroDOM *library*, drive the Playwright
page yourself, and wrap `to_compact_text()` / `selector_map()` as a few tools. Use these when you
already own the browser loop and only want the token-lean graph.

| file | shows |
|---|---|
| `openai_tool_calling.py` | the mechanics with no framework: a plain `while` loop over the Chat Completions `tools` API |
| `langchain_agent.py` | the same thing wrapped as three LangChain `@tool`s (`open_url`, `click`, `fill`) for `create_tool_calling_agent` |

```bash
pip install openai playwright   # or: langchain langchain-openai playwright
uv run playwright install chromium
OPENAI_API_KEY=... python examples/openai_tool_calling.py https://news.ycombinator.com "open the first story"
```

For Claude/Cursor instead of raw API calls, see the [MCP server](../README.md#10-second-mcp-setup) —
it's the same re-read-in-place pattern these scripts use, exposed as `zerodom_click_node` / `zerodom_fill_node`.

For the live-browser tool set (the Chrome extension + relay, `zerodom_click_node` and friends
driving your real, already-logged-in Chrome rather than a throwaway one), see
[`mcp_agent_system_prompt.md`](mcp_agent_system_prompt.md): a recommended system prompt for
an agent using those tools, checked against the real tool signatures.

For **bug-bounty / red-team hunting**, [`bug_bounty_hunt.md`](bug_bounty_hunt.md) is a
full methodology the agent follows to run the loop — recon (`zerodom crawl` + `scan --js`)
→ test IDOR / access-control / business-logic (`zerodom_compare_identities`, `zerodom_replay`)
→ report — on an authorized target, in your real logged-in session. Use it as a system
prompt or fill in its per-program block and use it as a `CLAUDE.md`.
