"""ZeroDOM actionable snapshot as a Crawl4AI ExtractionStrategy — before/after demo.

Crawls one page twice — once for Crawl4AI's own raw_markdown, once through the
ZeroDOM strategy — and prints the token count of each. The snapshot is the
actionable elements an agent acts on (click/fill), with selectors kept out of the
model context in `ref_map`; it's leaner because it drops prose by design.

    pip install 'zerodom[crawl4ai]' tiktoken
    python examples/crawl4ai_snapshot.py https://en.wikipedia.org/wiki/Web_scraping
"""

import asyncio
import json
import sys

import tiktoken
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig

from zerodom.crawl4ai import ZeroDOMExtractionStrategy

ENC = tiktoken.get_encoding("cl100k_base")
tokens = lambda s: len(ENC.encode(s or ""))


async def main(url: str) -> None:
    async with AsyncWebCrawler() as crawler:
        md = await crawler.arun(url)  # Crawl4AI's own pipeline → raw_markdown
        snap = await crawler.arun(
            url, config=CrawlerRunConfig(extraction_strategy=ZeroDOMExtractionStrategy())
        )

    raw = md.markdown.raw_markdown
    # Crawl4AI wraps a strategy's return in a JSON envelope; the snapshot is `content`.
    block = json.loads(snap.extracted_content)[0]
    actionable = block["content"]
    a, b = tokens(raw), tokens(actionable)
    print(f"\n{url}")
    print(f"  raw_markdown : {a:>7,} tokens")
    print(f"  zerodom snap : {b:>7,} tokens   ({(a - b) / a:.0%} fewer)")
    print(f"  selectors (ref_map, kept out of model context): {len(block.get('ref_map', {}))}\n")
    print(actionable[:600])


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://en.wikipedia.org/wiki/Web_scraping"
    asyncio.run(main(url))
