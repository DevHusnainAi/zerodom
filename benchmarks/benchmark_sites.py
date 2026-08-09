"""Drive ZeroDOM against a spread of real sites and score it honestly.

Per site: how many actionable nodes, how many of their selectors resolve to
exactly one live element (the only metric where being wrong is dangerous),
how many are ambiguous or dead, how many came back unlabelled, and what it
all costs in tokens.
"""
import json
import os
import pathlib
import sys
import time

import tiktoken
from playwright.sync_api import sync_playwright

from zerodom import ZeroDOM
from zerodom.frames import locate

FRAMES = os.environ.get("ZERODOM_FRAMES") == "1"

ENC = tiktoken.get_encoding("cl100k_base")
tok = lambda s: len(ENC.encode(s))

SITES = [
    # --- plain / content-first ---
    ("example.com", "https://example.com", "static"),
    ("news.ycombinator.com", "https://news.ycombinator.com", "static"),
    ("wikipedia (article)", "https://en.wikipedia.org/wiki/Web_browser", "static"),
    ("developer.mozilla.org", "https://developer.mozilla.org/en-US/docs/Web/HTML", "docs"),
    ("docs.python.org", "https://docs.python.org/3/library/asyncio.html", "docs"),
    ("lite.cnn.com", "https://lite.cnn.com", "static"),
    ("text.npr.org", "https://text.npr.org", "static"),
    ("danluu.com", "https://danluu.com", "static"),
    ("berkshirehathaway.com", "https://www.berkshirehathaway.com", "static"),
    ("gutenberg.org", "https://www.gutenberg.org", "static"),
    # --- SPA / JS-heavy marketing + docs ---
    ("react.dev", "https://react.dev", "spa"),
    ("nextjs.org", "https://nextjs.org", "spa"),
    ("vuejs.org", "https://vuejs.org", "spa"),
    ("svelte.dev", "https://svelte.dev", "spa"),
    ("angular.dev", "https://angular.dev", "spa"),
    ("tailwindcss.com", "https://tailwindcss.com", "spa"),
    ("vercel.com", "https://vercel.com", "spa"),
    ("cloudflare.com", "https://www.cloudflare.com", "spa"),
    ("stripe.com", "https://stripe.com", "spa"),
    ("figma.com", "https://www.figma.com", "spa"),
    # --- web components / shadow DOM ---
    ("youtube.com", "https://www.youtube.com", "shadow"),
    ("shoelace.style", "https://shoelace.style", "shadow"),
    ("material.angular.dev", "https://material.angular.dev", "shadow"),
    ("polymer-project", "https://polymer-library.polymer-project.org", "shadow"),
    ("github.io/lit", "https://lit.dev", "shadow"),
    # --- iframe-heavy ---
    ("codepen.io", "https://codepen.io", "iframe"),
    ("jsfiddle.net", "https://jsfiddle.net", "iframe"),
    ("w3schools.com", "https://www.w3schools.com/html/default.asp", "iframe"),
    ("replit.com", "https://replit.com", "iframe"),
    # --- canvas / graphics ---
    ("excalidraw.com", "https://excalidraw.com", "canvas"),
    ("photopea.com", "https://www.photopea.com", "canvas"),
    ("openstreetmap.org", "https://www.openstreetmap.org", "canvas"),
    # --- apps / dashboards / forms ---
    ("github.com/issues", "https://github.com/python/cpython/issues", "app"),
    ("gitlab.com", "https://gitlab.com/explore", "app"),
    ("old.reddit.com", "https://old.reddit.com", "app"),
    ("stackoverflow.com", "https://stackoverflow.com/questions", "app"),
    ("pypi.org", "https://pypi.org/project/zerodom/", "app"),
    ("archive.org", "https://archive.org", "app"),
    # --- e-commerce ---
    ("ebay.com", "https://www.ebay.com", "commerce"),
    ("etsy.com", "https://www.etsy.com", "commerce"),
    ("amazon.com", "https://www.amazon.com", "commerce"),
    ("bestbuy.com", "https://www.bestbuy.com", "commerce"),
    ("booking.com", "https://www.booking.com", "commerce"),
    ("airbnb.com", "https://www.airbnb.com", "commerce"),
    # --- government / accessibility-conscious ---
    ("gov.uk", "https://www.gov.uk", "gov"),
    ("usa.gov", "https://www.usa.gov", "gov"),
    ("nhs.uk", "https://www.nhs.uk", "gov"),
    ("irs.gov", "https://www.irs.gov", "gov"),
    # --- known auth / bot walls ---
    ("x.com", "https://x.com", "wall"),
    ("linkedin.com", "https://www.linkedin.com", "wall"),
    # --- more docs / reference ---
    ("readthedocs.io", "https://docs.readthedocs.io/en/stable/", "docs"),
    ("rust-lang.org/book", "https://doc.rust-lang.org/book/", "docs"),
    ("go.dev/doc", "https://go.dev/doc/", "docs"),
    ("kubernetes.io/docs", "https://kubernetes.io/docs/home/", "docs"),
    ("postgresql.org/docs", "https://www.postgresql.org/docs/current/index.html", "docs"),
    ("caniuse.com", "https://caniuse.com", "docs"),
    ("devdocs.io", "https://devdocs.io", "docs"),
    ("man7.org", "https://man7.org/linux/man-pages/index.html", "docs"),
    # --- more SPA / product ---
    ("linear.app", "https://linear.app", "spa"),
    ("notion.so", "https://www.notion.so", "spa"),
    ("supabase.com", "https://supabase.com", "spa"),
    ("railway.app", "https://railway.com", "spa"),
    ("sentry.io", "https://sentry.io", "spa"),
    ("datadoghq.com", "https://www.datadoghq.com", "spa"),
    ("openai.com", "https://openai.com", "spa"),
    ("anthropic.com", "https://www.anthropic.com", "spa"),
    ("huggingface.co", "https://huggingface.co", "spa"),
    ("netlify.com", "https://www.netlify.com", "spa"),
    ("astro.build", "https://astro.build", "spa"),
    ("remix.run", "https://remix.run", "spa"),
    # --- forms / auth / checkout-like ---
    ("gov.uk/log-in", "https://www.gov.uk/log-in-register-hmrc-online-services", "form"),
    ("wikipedia signup", "https://en.wikipedia.org/w/index.php?title=Special:CreateAccount", "form"),
    ("stripe docs checkout", "https://docs.stripe.com/payments/checkout", "form"),
    ("paypal.com", "https://www.paypal.com/us/home", "form"),
    ("mailchimp.com/signup", "https://mailchimp.com/signup/", "form"),
    ("surveymonkey.com", "https://www.surveymonkey.com", "form"),
    ("typeform.com", "https://www.typeform.com", "form"),
    ("jotform.com", "https://www.jotform.com", "form"),
    # --- more apps / dashboards ---
    ("bitbucket.org", "https://bitbucket.org/product", "app"),
    ("trello.com", "https://trello.com", "app"),
    ("asana.com", "https://asana.com", "app"),
    ("slack.com", "https://slack.com", "app"),
    ("discord.com", "https://discord.com", "app"),
    ("zoom.us", "https://www.zoom.com", "app"),
    ("dropbox.com", "https://www.dropbox.com", "app"),
    ("wordpress.com", "https://wordpress.com", "app"),
    ("shopify.com", "https://www.shopify.com", "app"),
    ("hn/newest", "https://news.ycombinator.com/newest", "app"),
    ("lobste.rs", "https://lobste.rs", "app"),
    ("sourcehut", "https://sr.ht", "app"),
    # --- more commerce ---
    ("walmart.com", "https://www.walmart.com", "commerce"),
    ("target.com", "https://www.target.com", "commerce"),
    ("ikea.com", "https://www.ikea.com/us/en/", "commerce"),
    ("newegg.com", "https://www.newegg.com", "commerce"),
    ("aliexpress.com", "https://www.aliexpress.com", "commerce"),
    ("wayfair.com", "https://www.wayfair.com", "commerce"),
    # --- more gov / institutional ---
    ("canada.ca", "https://www.canada.ca/en.html", "gov"),
    ("europa.eu", "https://european-union.europa.eu/index_en", "gov"),
    ("australia.gov.au", "https://www.australia.gov.au", "gov"),
    ("cdc.gov", "https://www.cdc.gov", "gov"),
    ("who.int", "https://www.who.int", "gov"),
    ("nasa.gov", "https://www.nasa.gov", "gov"),
    # --- media / heavy ---
    ("bbc.co.uk", "https://www.bbc.co.uk", "static"),
    ("theguardian.com", "https://www.theguardian.com/international", "static"),
    ("nytimes.com", "https://www.nytimes.com", "static"),
    ("arstechnica.com", "https://arstechnica.com", "static"),
    ("wikipedia main", "https://en.wikipedia.org/wiki/Main_Page", "static"),
    ("stackexchange.com", "https://stackexchange.com", "static"),
    # --- canvas / media apps ---
    ("tldraw.com", "https://www.tldraw.com", "canvas"),
    ("desmos.com", "https://www.desmos.com/calculator", "canvas"),
    ("google maps", "https://www.google.com/maps", "canvas"),
]

# Selectors using Playwright-only syntax, and anything inside a shadow root or an
# iframe, are unreachable from document.querySelectorAll. Auditing with it once
# reported every shadow node as dead; the audit goes through locators instead.
PW_ONLY = (":light(", ">> nth=")

# Nodes to probe for real actionability. Every node would be more honest, but each
# probe is a round trip; the sample exists to catch a systematic break.
ACTIONABLE_SAMPLE = 12


def audit(page, graph):
    """Resolution, plus a sample of genuine Playwright actionability.

    Resolving to exactly one element is necessary but not sufficient. A QA suite
    cares whether the click would land, which is what is_visible/is_enabled gate.
    """
    nodes = graph["nodes"]
    counts = []
    for n in nodes:
        try:
            counts.append(locate(page, n).count())
        except Exception:
            counts.append(-1)
    result = {
        "unique": sum(c == 1 for c in counts),
        "ambiguous": sum(c > 1 for c in counts),
        "dead": sum(c == 0 for c in counts),
        "invalid": sum(c == -1 for c in counts),
        "unlabelled": sum("Unlabelled" in n["label"] for n in nodes),
        "in_frame": sum(bool(n.get("frame")) for n in nodes),
    }
    step = max(1, len(nodes) // ACTIONABLE_SAMPLE)
    probed = actionable = 0
    for n in nodes[::step][:ACTIONABLE_SAMPLE]:
        try:
            loc = locate(page, n).first
            probed += 1
            actionable += bool(
                loc.is_visible(timeout=400) and loc.is_enabled(timeout=400)
            )
        except Exception:
            pass
    result["probed"], result["actionable"] = probed, actionable
    return result


def main(out_path, limit=99):
    done = set()
    if pathlib.Path(out_path).exists():
        done = {json.loads(l)["site"] for l in open(out_path)}
    out = open(out_path, "a", buffering=1)
    todo = [s for s in SITES if s[0] not in done][:limit]
    print(f"# {len(done)} done, running {len(todo)}", flush=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, url, kind in todo:
            row = {"site": name, "url": url, "kind": kind}
            ctx = None
            try:
                ctx = browser.new_context(
                    viewport={"width": 1366, "height": 900},
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                )
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)  # let hydration settle
                t0 = time.perf_counter()
                graph = ZeroDOM.from_page(page, frames=FRAMES)
                row["parse_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                compact = graph.to_compact_text()
                row["nodes"] = len(graph["nodes"])
                row["tokens"] = tok(compact)
                row["raw_tokens"] = tok(page.content())
                row["tok_per_node"] = (
                    round(row["tokens"] / row["nodes"], 1) if row["nodes"] else None
                )
                # "identical output every run" is a product claim; verify it.
                repeat = ZeroDOM.from_page(page, frames=FRAMES)
                key = lambda g: [
                    (n["type"], n["label"], n["selector"]) for n in g["nodes"]
                ]
                row["deterministic"] = key(repeat) == key(graph)
                row["warning"] = graph["metadata"].get("warning", "")[:70]
                row.update(audit(page, graph))
                row["shadow_sel"] = sum(
                    any(m in n["selector"] for m in PW_ONLY) for n in graph["nodes"]
                )
                row["title"] = graph["metadata"]["page_title"][:60]
            except Exception as e:
                row["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            finally:
                if ctx:
                    try:
                        ctx.close()
                    except Exception:
                        pass
            out.write(json.dumps(row) + "\n")
            print(json.dumps(row), flush=True)
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 99)
