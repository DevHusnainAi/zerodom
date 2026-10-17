"""ZeroDOM in the OpenAI Agents SDK — the full MCP toolset over stdio.

    pip install openai-agents
    uvx --from zerodom playwright install chromium          # once, if it will drive a browser
    OPENAI_API_KEY=... python examples/integrations/openai_agents.py
"""
from __future__ import annotations

import asyncio

from agents import Agent, Runner
from agents.mcp import MCPServerStdio

TASK = (
    "You are testing an app you are authorized to assess. Register two logged-in identities from "
    "./user.json and ./admin.json, then compare https://app.example.com/api/admin/dashboard across "
    "both. A 200-vs-401/403 split on the identical request is a broken-access-control (IDOR) lead — "
    "report it with the status split and the only_<identity> controls. Surface the tell, don't claim "
    "a confirmed bug."
)


async def main() -> None:
    # The `async with` keeps ONE zerodom-mcp process alive for the whole run, so the browser session
    # and registered identities survive across tool calls — run the agent INSIDE the context.
    # client_session_timeout_seconds is bumped because crawl/sweep can take a while.
    async with MCPServerStdio(
        params={"command": "uvx", "args": ["--from", "zerodom", "zerodom-mcp"]},
        cache_tools_list=True,
        client_session_timeout_seconds=120,
    ) as zerodom:
        agent = Agent(
            name="ZeroDOM hunter",
            model="gpt-4.1",  # any model the SDK supports
            instructions="You run authorized AppSec tests using the zerodom tools.",
            mcp_servers=[zerodom],
        )
        result = await Runner.run(agent, TASK)
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
