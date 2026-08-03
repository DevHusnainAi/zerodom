"""Token-savings benchmark across real pages (acceptance criterion 1).

    uv run python benchmarks/benchmark_tokens.py [url ...] [--render]
"""

from __future__ import annotations

import sys
import time

from zerodom.cli import count_tokens, fetch
from zerodom.parser import ZeroDOMParser

DEFAULT_URLS = [
    "https://www.airbnb.com/",
    "https://github.com/microsoft/playwright/issues",
    "https://en.wikipedia.org/wiki/Neural_network",
    "https://news.ycombinator.com",
    "https://developer.mozilla.org/en-US/",
]


def main(argv: list[str]) -> int:
    render = "--render" in argv
    urls = [a for a in argv if not a.startswith("--")] or DEFAULT_URLS

    print(
        f"{'url':<45} {'raw':>10} {'json':>9} {'compact':>9} "
        f"{'json%':>7} {'compact%':>9} {'nodes':>6} {'ms':>6}"
    )
    print("─" * 108)
    savings = []
    for url in urls:
        try:
            html, final_url = fetch(url, render)
        except Exception as exc:  # network/bot-wall failures shouldn't kill the run
            print(f"{url:<45} {type(exc).__name__}: {exc}")
            continue
        start = time.perf_counter()
        graph = ZeroDOMParser(html, final_url).parse()
        elapsed = (time.perf_counter() - start) * 1000

        raw = count_tokens(html)
        as_json = count_tokens(graph.to_json(indent=None))
        as_text = count_tokens(graph.to_compact_text())
        pct = lambda n: (1 - n / raw) * 100 if raw else 0.0  # noqa: E731
        savings.append(pct(as_text))
        # A few hundred tokens means a bot wall or consent gate, not a real page.
        flag = "  <- likely a bot wall (try --render)" if raw < 2000 else ""
        print(
            f"{url[:44]:<45} {raw:>10,} {as_json:>9,} {as_text:>9,} "
            f"{pct(as_json):>6.1f}% {pct(as_text):>8.1f}% "
            f"{graph['metadata']['total_interactive_nodes']:>6} {elapsed:>5.1f}{flag}"
        )

    if savings:
        print("─" * 108)
        print(
            f"mean compact savings {sum(savings) / len(savings):.1f}%  "
            f"(criterion: >= 80%, worst page {min(savings):.1f}%)"
        )
    return 0 if savings and min(savings) >= 80 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
