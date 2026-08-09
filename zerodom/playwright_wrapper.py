"""Playwright middleware adapter."""

from __future__ import annotations

import inspect
from typing import Any

from .parser import InteractionGraph, ZeroDOMParser

# `page.content()` serializes light DOM only, so controls inside a shadow root are
# invisible — and worse, Playwright's CSS engine *pierces* open shadow roots when it
# clicks, so a light-DOM path like `#host > button` silently matches shadow elements
# we never knew were there. Chromium's getHTML() emits open roots as
# `<template shadowrootmode>`, which gives the parser the whole picture.
#
# Closed roots stay unreachable by design; no API exposes them.
SERIALIZE = """() => {
  const roots = [];
  const marked = [];
  const visit = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) { roots.push(el.shadowRoot); visit(el.shadowRoot); }
      // A stylesheet rule is invisible to a parser reading HTML text, so
      // `<input class="hidden">` looks clickable and an agent burns a 30s
      // timeout on it. Only the browser knows; record what it knows.
      //
      // NOT contentVisibilityAuto: `content-visibility: auto` is a rendering
      // optimisation for offscreen content, not a way to hide it. Counting it
      // as hidden deletes everything below the fold — on vercel.com that was
      // 129 of 177 controls, an agent blinded to most of the page.
      if (!el.checkVisibility({ visibilityProperty: true })) {
        el.setAttribute('data-zerodom-hidden', '');
        marked.push(el);
      }
    }
  };
  visit(document);
  const html = document.documentElement;
  if (!html.getHTML) { for (const el of marked) el.removeAttribute('data-zerodom-hidden'); return null; }
  try {
    return '<html' + [...html.attributes].map(a => ` ${a.name}="${a.value}"`).join('')
         + '>' + html.getHTML({ serializableShadowRoots: true, shadowRoots: roots })
         + '</html>';
  } finally {
    // The page belongs to the caller; leave it exactly as we found it. This
    // whole function is synchronous, so nothing else can observe the marks.
    for (const el of marked) el.removeAttribute('data-zerodom-hidden');
  }
}"""


def serialize(page: Any) -> str:
    """Page HTML including open shadow roots. Sync pages only — see `from_page`."""
    return page.evaluate(SERIALIZE) or page.content()


async def serialize_async(page: Any) -> str:
    """`serialize` for async pages. Separate because `x or await y` would treat the
    un-awaited coroutine as truthy and never reach the fallback."""
    return await page.evaluate(SERIALIZE) or await page.content()


class ZeroDOM:
    """1-line Playwright integration: `ZeroDOM.from_page(page)`."""

    @staticmethod
    def from_html(html: str, url: str = "about:blank") -> InteractionGraph:
        return ZeroDOMParser(html, url).parse()

    @staticmethod
    def from_page(page: Any) -> InteractionGraph | Any:
        """Parse a Playwright page.

        Works with both APIs: sync pages return the graph directly, async pages
        return an awaitable that resolves to it.
        """
        # Duck-typed on purpose — anything with `.content()` and `.url` parses. Shadow
        # serialization also needs `.evaluate()`, and returns None when the page has no
        # shadow roots, so both paths fall back to the plain light-DOM content.
        pending = page.evaluate(SERIALIZE) if hasattr(page, "evaluate") else None
        if inspect.isawaitable(pending):
            return _from_async_page(pending, page)
        content = pending or page.content()
        if inspect.isawaitable(content):
            return _from_async_page(content, page)
        return ZeroDOMParser(content, page.url).parse()


async def _from_async_page(pending: Any, page: Any) -> InteractionGraph:
    html = await pending or await page.content()
    return ZeroDOMParser(html, page.url).parse()
