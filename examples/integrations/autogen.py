"""ZeroDOM in a Microsoft AutoGen agent — the full MCP toolset over stdio.

    pip install "autogen-ext[mcp]" autogen-agentchat "mcp<2"   # see the mcp-pin note below
    uvx --from zerodom playwright install chromium             # once, if it will drive a browser
    OPENAI_API_KEY=... python examples/integrations/autogen.py

mcp-pin: autogen-ext (<=0.7.5) targets the mcp 1.x SDK; the 2.x SDK removed symbols it imports
(ImportError: RequestContext). Pin "mcp<2" until autogen-ext updates. Tool loading is
runtime-verified against zerodom-mcp (39 tools).
"""
from __future__ import annotations

import asyncio

from autogen_agentchat.agents import AssistantAgent
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_ext.tools.mcp import StdioServerParams, mcp_server_tools

TASK = (
    "On an app you are authorized to assess, register two logged-in identities from ./user.json and "
    "./admin.json, then compare https://app.example.com/api/admin/dashboard across both. A "
    "200-vs-401/403 split on the identical request is a broken-access-control (IDOR) lead: report it "
    "with the status split and the only_<identity> controls. Surface the tell, don't claim a bug."
)


async def main() -> None:
    params = StdioServerParams(
        command="uvx",
        args=["--from", "zerodom", "zerodom-mcp"],
        read_timeout_seconds=120,  # crawl/sweep can take a while
    )
    # For a multi-step hunt prefer McpWorkbench (keeps one session); mcp_server_tools is the
    # simple form shown here. Either way, keep one process for the run (see ./README.md).
    tools = await mcp_server_tools(params)
    print(f"loaded {len(tools)} zerodom tools")

    agent = AssistantAgent(
        name="zerodom_hunter",
        model_client=OpenAIChatCompletionClient(model="gpt-4.1"),  # any AutoGen model client
        tools=tools,
        system_message="You run authorized AppSec tests using the zerodom tools.",
    )
    print((await agent.run(task=TASK)).messages[-1].content)


if __name__ == "__main__":
    asyncio.run(main())
