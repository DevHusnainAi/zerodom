"""Minimal tool-calling loop: OpenAI picks a node from the ZeroDOM graph, we act on it.

Run:
    pip install openai playwright
    uv run playwright install chromium
    OPENAI_API_KEY=... python examples/openai_tool_calling.py https://news.ycombinator.com "open the first story"

No framework — just the Chat Completions tools API and a plain while loop, to show
the mechanics that langchain_agent.py wraps in a framework.
"""

from __future__ import annotations

import json
import sys

from openai import OpenAI
from playwright.sync_api import sync_playwright

from zerodom import ZeroDOM

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click an interactive node by its id, e.g. '03'.",
            "parameters": {
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fill",
            "description": "Type text into a fillable node by its id.",
            "parameters": {
                "type": "object",
                "properties": {"node_id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["node_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Call once the task is complete.",
            "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}},
        },
    },
]


def run(url: str, task: str, max_steps: int = 8) -> None:
    client = OpenAI()
    with sync_playwright() as pw:
        page = pw.chromium.launch().new_page()
        page.goto(url, wait_until="domcontentloaded")

        def read() -> tuple[str, dict[str, str]]:
            graph = ZeroDOM.from_page(page)
            return graph.to_compact_text(), graph.selector_map()

        text, selectors = read()
        messages = [
            {"role": "system", "content": "You control a browser via node ids from a page graph."},
            {"role": "user", "content": f"Task: {task}\n\n{text}"},
        ]

        for _ in range(max_steps):
            resp = client.chat.completions.create(model="gpt-5", messages=messages, tools=TOOLS)
            msg = resp.choices[0].message
            messages.append(msg)

            if not msg.tool_calls:
                print(msg.content)
                return

            for call in msg.tool_calls:
                args = json.loads(call.function.arguments)
                if call.function.name == "done":
                    print("done:", args.get("summary"))
                    return

                selector = selectors[f"node_{args['node_id'].strip('[]').zfill(2)}"]
                if call.function.name == "click":
                    page.click(selector)
                else:
                    page.fill(selector, args["text"])

                text, selectors = read()
                messages.append({"role": "tool", "tool_call_id": call.id, "content": text})


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
