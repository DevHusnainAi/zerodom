"""ZeroDOM in a CrewAI crew — the full MCP toolset over stdio.

    pip install "crewai-tools[mcp]" crewai
    uvx --from zerodom playwright install chromium          # once, if it will drive a browser
    OPENAI_API_KEY=... python examples/integrations/crewai.py
"""
from __future__ import annotations

import os

from crewai import Agent, Crew, Task
from crewai_tools import MCPServerAdapter
from mcp import StdioServerParameters

server = StdioServerParameters(
    command="uvx",
    args=["--from", "zerodom", "zerodom-mcp"],
    env={**os.environ},  # pass the shell env through (API keys, PATH, …)
)

TASK = (
    "On an app you are authorized to assess, register two logged-in identities from ./user.json and "
    "./admin.json, then compare https://app.example.com/api/admin/dashboard across both. A "
    "200-vs-401/403 split on the identical request is a broken-access-control (IDOR) lead: report it "
    "with the status split and the only_<identity> controls. Surface the tell, don't claim a bug."
)

# The `with` block keeps ONE zerodom-mcp process alive for the crew — build and kickoff INSIDE it,
# so the browser session and registered identities persist across tool calls.
with MCPServerAdapter(server) as tools:
    hunter = Agent(
        role="AppSec tester",
        goal="Find broken access control between two identities",
        backstory="A meticulous application-security engineer running authorized tests.",
        tools=tools,
        verbose=True,
    )
    task = Task(
        description=TASK,
        agent=hunter,
        expected_output="An IDOR verdict with the 200-vs-401 status split, or a clear 'no finding'.",
    )
    print(Crew(agents=[hunter], tasks=[task]).kickoff())
