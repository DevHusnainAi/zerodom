# Examples

Two ways to plug ZeroDOM into a tool-calling agent. Both do the same thing —
parse the page, hand the model `to_compact_text()`, resolve whatever node id it
picks back to a selector server-side — one via LangChain, one raw.

| file | shows |
|---|---|
| `openai_tool_calling.py` | the mechanics with no framework: a plain `while` loop over the Chat Completions `tools` API |
| `langchain_agent.py` | the same thing wrapped as three LangChain `@tool`s (`open_url`, `click`, `fill`) for `create_tool_calling_agent` |

```bash
pip install openai playwright   # or: langchain langchain-openai playwright
uv run playwright install chromium
OPENAI_API_KEY=... python examples/openai_tool_calling.py https://news.ycombinator.com "open the first story"
```

For Claude/Cursor instead of raw API calls, see the [MCP server](../README.md#claude-mcp-setup) —
it's the same re-read-in-place pattern these scripts use, exposed as `zerodom_click_node` / `zerodom_fill_node`.
