"""ZeroDOM in a Google ADK agent — the full MCP toolset over stdio.

    pip install google-adk mcp
    uvx --from zerodom playwright install chromium          # once, if it will drive a browser
    # set the model's credentials, e.g. GOOGLE_API_KEY for Gemini
    python examples/integrations/google_adk.py

Tool loading (MCPToolset.get_tools) is runtime-verified against zerodom-mcp (39 tools).
"""
from __future__ import annotations

import asyncio

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import (
    MCPToolset,
    StdioConnectionParams,
    StdioServerParameters,
)

# One MCPToolset instance manages a single connection for the agent's whole life — so zerodom-mcp's
# browser session and registered identities persist across tool calls (see ./README.md). ADK closes
# it gracefully when the app terminates.
zerodom = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(command="uvx", args=["--from", "zerodom", "zerodom-mcp"]),
        timeout=120,  # crawl/sweep can take a while
    )
)

agent = LlmAgent(
    model="gemini-2.0-flash",  # any model ADK supports
    name="zerodom_hunter",
    instruction="You run authorized AppSec tests using the zerodom tools.",
    tools=[zerodom],
)


async def _list_tools() -> None:
    # Minimal check: the toolset actually exposes zerodom's tools. Drive `agent` with ADK's
    # Runner / InMemoryRunner to run the full hunt — see https://google.github.io/adk-docs/.
    tools = await zerodom.get_tools()
    print(f"loaded {len(tools)} zerodom tools; agent '{agent.name}' is wired")
    await zerodom.close()


if __name__ == "__main__":
    asyncio.run(_list_tools())
