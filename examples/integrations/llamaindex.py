"""ZeroDOM in a LlamaIndex agent — the full MCP toolset over stdio.

    pip install llama-index-tools-mcp llama-index llama-index-llms-anthropic
    uvx --from zerodom playwright install chromium          # once, if it will drive a browser
    ANTHROPIC_API_KEY=... python examples/integrations/llamaindex.py

Tool loading (BasicMCPClient + McpToolSpec) is runtime-verified against zerodom-mcp (35 tools).
"""
from __future__ import annotations

import asyncio

from llama_index.core.agent.workflow import FunctionAgent
from llama_index.llms.anthropic import Anthropic  # or any LlamaIndex LLM
from llama_index.tools.mcp import BasicMCPClient, McpToolSpec

TASK = (
    "On an app you are authorized to assess, register two logged-in identities from ./user.json and "
    "./admin.json, then compare https://app.example.com/api/admin/dashboard across both. A "
    "200-vs-401/403 split on the identical request is a broken-access-control (IDOR) lead: report it "
    "with the status split and the only_<identity> controls. Surface the tell, don't claim a bug."
)


async def main() -> None:
    # One BasicMCPClient for the run — keep this single instance so zerodom-mcp's browser session
    # and registered identities persist across the agent's tool calls (see ./README.md).
    client = BasicMCPClient("uvx", args=["--from", "zerodom", "zerodom-mcp"])
    tools = await McpToolSpec(client=client).to_tool_list_async()
    print(f"loaded {len(tools)} zerodom tools")

    agent = FunctionAgent(
        tools=tools,
        llm=Anthropic(model="claude-sonnet-4-5"),  # any LlamaIndex LLM; set its API key
        system_prompt="You run authorized AppSec tests using the zerodom tools.",
    )
    print(await agent.run(TASK))


if __name__ == "__main__":
    asyncio.run(main())
