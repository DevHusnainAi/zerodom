"""Expose ZeroDOM as LangChain tools so any tool-calling agent can act on a page.

Run:
    pip install langchain langchain-openai playwright
    uv run playwright install chromium
    OPENAI_API_KEY=... python examples/langchain_agent.py "go to https://news.ycombinator.com and open the first story"

Mirrors the re-read pattern in zerodom/mcp_server.py: every action re-parses the
live page rather than re-navigating, so node ids stay valid and typed state survives.
"""

from __future__ import annotations

import sys

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from playwright.sync_api import sync_playwright

from zerodom import ZeroDOM

_pw = sync_playwright().start()
_page = _pw.chromium.launch().new_page()
_selectors: dict[str, str] = {}


def _read() -> str:
    global _selectors
    graph = ZeroDOM.from_page(_page)
    _selectors = graph.selector_map()
    return graph.to_compact_text()


def _selector(node_id: str) -> str:
    return _selectors[f"node_{node_id.strip('[]').zfill(2)}"]


@tool
def open_url(url: str) -> str:
    """Navigate to a URL and return the page's interaction graph."""
    _page.goto(url, wait_until="domcontentloaded")
    return _read()


@tool
def click(node_id: str) -> str:
    """Click a node id from the last graph, e.g. '03'. Returns the refreshed graph."""
    _page.click(_selector(node_id))
    return _read()


@tool
def fill(node_id: str, text: str) -> str:
    """Type text into a fillable node id. Returns the refreshed graph."""
    _page.fill(_selector(node_id), text)
    return _read()


tools = [open_url, click, fill]
prompt = ChatPromptTemplate.from_messages(
    [
        ("system", "You control a browser through node ids from a ZeroDOM page graph."),
        ("human", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ]
)
agent = create_tool_calling_agent(ChatOpenAI(model="gpt-5"), tools, prompt)
executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

if __name__ == "__main__":
    task = sys.argv[1] if len(sys.argv) > 1 else "Go to https://news.ycombinator.com and open the first story."
    executor.invoke({"input": task})
