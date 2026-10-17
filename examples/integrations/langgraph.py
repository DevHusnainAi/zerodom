"""ZeroDOM in a LangGraph agent — the FULL MCP toolset, not hand-wrapped tools.

Unlike ../langchain_agent.py (which wraps only open/click/fill from the ZeroDOM *library*),
this loads every zerodom_* tool straight off the MCP server — including the authz weapons
zerodom_add_identity / zerodom_compare_identities / zerodom_replay / zerodom_crawl.

    pip install "langchain-mcp-adapters>=0.1" langgraph langchain langchain-anthropic
    uvx --from zerodom playwright install chromium          # once, if it will drive a browser
    ANTHROPIC_API_KEY=... python examples/integrations/langgraph.py
"""
from __future__ import annotations

import asyncio

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.prebuilt import create_react_agent

# The one stdio config every MCP-capable framework reuses. Swap for {"command": "zerodom-mcp"}
# if it's installed, or run `zerodom relay` + the extension first to drive your real Chrome.
ZERODOM = {"zerodom": {"command": "uvx", "args": ["--from", "zerodom", "zerodom-mcp"], "transport": "stdio"}}

MODEL = "anthropic:claude-sonnet-4-5"  # any model LangChain supports; set the matching API key
TASK = (
    "You are testing an app you are authorized to assess. Register two logged-in identities from "
    "./user.json and ./admin.json, then zerodom_compare_identities on "
    "https://app.example.com/api/admin/dashboard. If one gets 200 where the other gets 401/403 on "
    "the identical request, report a broken-access-control (IDOR) lead with the status split and the "
    "only_<identity> controls. Surface the tell — do not claim a confirmed bug."
)


async def main() -> None:
    client = MultiServerMCPClient(ZERODOM)

    # ponytail: a PERSISTENT session, not the stateless default. zerodom-mcp keeps ONE browser
    # session + the registered identities in-process; the stateless path opens a fresh MCP session
    # per tool call, dropping the page/identities between steps. client.session(...) keeps a single
    # zerodom-mcp process alive for the whole agent run.
    async with client.session("zerodom") as session:
        tools = await load_mcp_tools(session)
        print(f"loaded {len(tools)} zerodom tools")
        agent = create_react_agent(MODEL, tools)
        out = await agent.ainvoke({"messages": [{"role": "user", "content": TASK}]})
        print(out["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
