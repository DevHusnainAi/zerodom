"""Crawl4AI adapter: a token-lean, actionable snapshot as an ExtractionStrategy.

Opt-in. Importing this module needs crawl4ai installed (it is not a zerodom
dependency) — the same shape as crawl4ai's own LLM strategies: you only reach
for the adapter if you already have the thing it adapts.

    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
    from zerodom.crawl4ai import ZeroDOMExtractionStrategy

    cfg = CrawlerRunConfig(extraction_strategy=ZeroDOMExtractionStrategy())
    result = await crawler.arun(url, config=cfg)
    snapshot = result.extracted_content   # [01] input Password filled ...

Crawl4AI hands the strategy already-rendered HTML, so state comes from the
static DOM that HTML exposes (aria/native attributes, serialized values);
live-only DOM properties set purely in JS after render aren't visible to an
HTML-only parse. Selectors are returned in `ref_map`, kept out of the model's
context so a downstream agent acts on what it read.
"""

from __future__ import annotations

from crawl4ai.extraction_strategy import ExtractionStrategy

from .parser import parse_html


class ZeroDOMExtractionStrategy(ExtractionStrategy):
    """Return ZeroDOM's compact interaction graph as `extracted_content`."""

    def __init__(self, viewport_only: bool = False, with_selectors: bool = True, **kwargs):
        kwargs["input_format"] = "html"  # ZeroDOM parses HTML, not markdown/text
        super().__init__(**kwargs)
        self.viewport_only = viewport_only
        self.with_selectors = with_selectors

    def extract(self, url, html, *args, **kwargs):
        g = parse_html(html or "", url=url or "about:blank", viewport_only=self.viewport_only)
        block = {
            "index": 0,
            "tags": ["zerodom.snapshot"],
            "content": g.to_compact_text(terse=True),
        }
        if self.with_selectors:
            block["ref_map"] = g.ref_map()  # {"01": "body > a", ...} — stays caller-side
        return [block]
