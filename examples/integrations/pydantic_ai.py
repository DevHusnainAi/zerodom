"""ZeroDOM in a Pydantic AI agent — the full MCP toolset over stdio.

API note: pydantic-ai >= 1.0 uses MCPToolset + fastmcp's StdioTransport (below). On older
versions it was `from pydantic_ai.mcp import MCPServerStdio` + `Agent(..., mcp_servers=[server])`
+ `async with agent.run_mcp_servers():`.

    pip install pydantic-ai
    uvx --from zerodom playwright install chromium          # once, if it will drive a browser
    ANTHROPIC_API_KEY=... python examples/integrations/pydantic_ai.py
"""
from __future__ import annotations

import asyncio

from fastmcp.client.transports import StdioTransport
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset

MODEL = "anthropic:claude-sonnet-4-5"  # any model Pydantic AI supports; set the matching API key
TASK = (
    "On an app you are authorized to assess, register two logged-in identities from ./user.json and "
    "./admin.json, then compare https://app.example.com/api/admin/dashboard across both. A "
    "200-vs-401/403 split on the identical request is a broken-access-control (IDOR) lead: report it "
    "with the status split and the only_<identity> controls. Surface the tell, don't claim a bug."
)

zerodom = MCPToolset(StdioTransport(command="uvx", args=["--from", "zerodom", "zerodom-mcp"]))
agent = Agent(MODEL, toolsets=[zerodom])


async def main() -> None:
    # `async with agent` opens the toolset once and keeps the zerodom-mcp subprocess alive for the
    # whole run, so the browser session and registered identities survive across tool calls.
    async with agent:
        result = await agent.run(TASK)
        print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
